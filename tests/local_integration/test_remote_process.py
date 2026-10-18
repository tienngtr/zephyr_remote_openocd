# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import array
import fcntl
import hashlib
import io
import json
import os
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, suppress
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import Any, BinaryIO, override
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.controller import _forwarding as forwarding_module
from zephyr_remote_openocd.controller import _rtt as rtt_module
from zephyr_remote_openocd.controller import helper_connection as connection_module
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._forwarding import ForwardManager
from zephyr_remote_openocd.controller._rtt import RttClientError, run_rtt_client
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    SshCommand,
    SshForwardSpec,
    stop_process,
)
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionAllocation,
    SessionController,
    SessionError,
    SessionPlan,
    StagedDirectory,
    StagedFile,
    query_remote_openocd_version,
)
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.paths import REMOTE_ADDRESS_PLACEHOLDER, PathPlanner
from zephyr_remote_openocd.controller.staging import build_archive
from zephyr_remote_openocd.controller.waiting import WaitSources
from zephyr_remote_openocd.helper import _workspace as workspace_module
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy, Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import ProtocolError, decode_terminal, encode_frame

from tests.control_support import ControlProcess
from tests.process_support import cleanup_on_exit, local_transport, read_line, read_lines
from tests.protocol_support import terminal_message, terminal_snapshot
from tests.session_support import NotifiedAuthority, ObservedStatus
from tests.support import ROOT

SCRIPTED_HELPER = ROOT / "tests/fixtures/scripted_helper.py"

TEST_PROCESS = ProcessSpec(("test-process",))
OPENOCD_FAILURE_RC = 7
HELPER_FAILURE_RC = 9


class _BlockedSshCommand(SshCommand):
    """SSH test double that fails unless a test overrides the operation."""

    @override
    def run(
        self,
        host: str,
        remote_command: str,
        *,
        input_data: bytes | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("run() is not expected in this test")

    @override
    def popen(
        self,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired=None,
    ) -> ManagedSshProcess:
        raise AssertionError("popen() is not expected in this test")

    @override
    def run_stream(
        self,
        host: str,
        remote_command: str,
        stream: BinaryIO,
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("run_stream() is not expected in this test")


@pytest.mark.parametrize("phase", ("before-control", "control-launch", "allocation", "ready"))
def test_interrupted_helper_acquisition_cleans_owned_control_process(tmp_path, monkeypatch, phase):
    helper_path = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
    environment = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)}
    managed_processes: list[ManagedSshProcess] = []
    workspaces: list[Path] = []
    interruption = KeyboardInterrupt()
    original_acquire = HelperConnection.acquire
    original_created = HelperConnection._created

    class LocalCommand(_BlockedSshCommand):
        @override
        def popen(self, host, remote_command, *, local_forward=None, on_acquired=None):
            del host, remote_command
            assert local_forward is None
            managed = local_transport(
                [sys.executable, str(helper_path), "control"],
                environment=environment,
                on_acquired=on_acquired,
            )
            managed_processes.append(managed)
            if phase == "control-launch":
                raise interruption
            return managed

    def observe_and_interrupt(snapshot):
        allocation = original_created(snapshot)
        if allocation is not None:
            workspaces.append(Path(allocation.remote_workspace))
            if phase == "allocation":
                raise interruption
        return allocation

    def acquire_and_interrupt(client):
        if phase == "before-control":
            raise interruption
        original_acquire(client)
        if phase == "ready":
            raise interruption

    monkeypatch.setattr(HelperConnection, "acquire", acquire_and_interrupt)
    monkeypatch.setattr(HelperConnection, "_created", staticmethod(observe_and_interrupt))
    session = SessionController(
        SessionPlan("local", LocalCommand(), TEST_PROCESS),
        DeploymentResult(str(helper_path), "test", False),
    )
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            try:
                session.acquire()
            finally:
                session.close()

        assert raised.value is interruption
        assert session.closed
        assert all(process.poll() is not None for process in managed_processes)
        assert all(not workspace.exists() for workspace in workspaces)
        for managed in managed_processes:
            assert managed.stdin is not None and managed.stdin.closed
            assert managed.stdout is not None and managed.stdout.closed
    finally:
        for managed in managed_processes:
            stop_process(managed)


def test_interruption_after_reader_start_keeps_one_owned_reader(monkeypatch, notified_authority):
    created = encode_frame(
        "SESSION_CREATED", helper="test", session_id="session", remote_workspace="/workspace"
    )
    code = f"import os,sys\nos.write(1, {created!r})\nsys.stdin.buffer.read()\n"
    authority = notified_authority.authority
    helper = HelperConnection(
        SshCommand((sys.executable, "-u", "-c", code)),
        "local",
        DeploymentResult("/helper.py", "digest", False),
        authority=authority,
        notifier=notified_authority.notifier,
    )
    started: list[threading.Thread] = []
    start = threading.Thread.start
    interruption = KeyboardInterrupt()

    def interrupt_reader_start(thread: threading.Thread) -> None:
        start(thread)
        if thread.name == "zro-helper-events":
            # Arbitrary BaseException immediately after the started reader's handoff.
            started.append(thread)
            raise interruption

    monkeypatch.setattr(threading.Thread, "start", interrupt_reader_start)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            helper.acquire()
        helper.close()

        assert raised.value is interruption
        assert authority.snapshot().failure is interruption
        assert len(started) == 1
        assert not started[0].is_alive()
    finally:
        with suppress(BaseException):
            helper.close()
        for reader in started:
            reader.join(timeout=30)
            assert not reader.is_alive()


@pytest.mark.parametrize(
    ("openocd_returncode", "transport_timeout"),
    ((0, False), (OPENOCD_FAILURE_RC, False), (0, True)),
    ids=("natural-success", "natural-failure", "transport-timeout"),
)
def test_helper_close_allows_recorded_natural_exit_before_termination(
    monkeypatch, notified_authority, openocd_returncode, transport_timeout
):
    exit_read, exit_write = os.pipe()
    terminal_read, terminal_write = os.pipe()
    created = encode_frame(
        "SESSION_CREATED", helper="test", session_id="session", remote_workspace="/workspace"
    )
    ready = encode_frame("ATTEMPT", argv=["test-process"]) + encode_frame(
        "READY", remote_address="127.64.0.1", child_pid=1
    )
    terminal = terminal_message(Trigger.CHILD_EXIT, returncode=openocd_returncode)
    code = (
        "import os, sys\n"
        f"os.write(1, {created!r})\n"
        "sys.stdin.buffer.readline()\n"
        f"os.write(1, {ready!r})\n"
        f"os.read({terminal_read}, 1)\n"
        f"os.write(1, {terminal!r})\n"
        f"os.close(1)\nos.close(2)\nos.read({exit_read}, 1)\n"
    )
    managed = local_transport([sys.executable, "-c", code], pass_fds=(exit_read, terminal_read))
    os.close(exit_read)
    os.close(terminal_read)

    class LocalCommand(_BlockedSshCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired=None,
        ) -> ManagedSshProcess:
            acquired_process = managed
            if on_acquired is not None:
                on_acquired(acquired_process)
            return acquired_process

    terminated = False
    graceful_wait_started = False
    wait = managed.wait
    terminate = managed.terminate

    def observe_terminate() -> None:
        nonlocal terminated
        terminated = True
        terminate()

    def gated_wait(timeout: float | None = None) -> int:
        nonlocal graceful_wait_started
        if not terminated:
            assert timeout is not None and 0 < timeout <= connection_module.HELPER_STOP_TIMEOUT
            graceful_wait_started = True
            if transport_timeout:
                raise subprocess.TimeoutExpired(managed.args, timeout)
            os.write(exit_write, b"x")
        return wait(timeout=timeout)

    authority = notified_authority.authority
    try:
        helper = HelperConnection(
            LocalCommand(),
            "local",
            DeploymentResult("/helper.py", "digest", False),
            authority=authority,
            notifier=notified_authority.notifier,
        )
        helper.acquire()
        assert helper.start_process(TEST_PROCESS, ()) == "127.64.0.1"
        os.write(terminal_write, b"x")
        # EOF and protocol completion are observed while OS exit remains gated.
        assert helper._join_reader(timeout=5)
        helper.observe_status()
        initial_failure = authority.snapshot().failure
        if openocd_returncode:
            with pytest.raises(SessionError) as observed:
                authority.operation_status()
        else:
            assert authority.operation_status() == 0
        assert managed.poll() is None
        monkeypatch.setattr(managed, "wait", gated_wait)
        monkeypatch.setattr(managed, "terminate", observe_terminate)

        helper.close()
        error = authority.snapshot().failure

        assert graceful_wait_started
        assert terminated is transport_timeout
        assert managed.returncode is not None
        if transport_timeout:
            if openocd_returncode:
                assert error is observed.value
                assert any("timed out" in note for note in error.__notes__)
            else:
                assert isinstance(error, subprocess.TimeoutExpired)
        else:
            assert managed.returncode == 0
            assert error is initial_failure
        terminal = authority.snapshot().terminal
        assert terminal is not None
        child_result = terminal.outcome.child_result
        assert (None if child_result is None else child_result.returncode) == openocd_returncode
        assert managed.stdin is not None and managed.stdin.closed
        assert managed.stdout is not None and managed.stdout.closed
    finally:
        os.close(exit_write)
        os.close(terminal_write)
        if managed.poll() is None:
            managed.kill()
        wait(timeout=5)
        managed.close_stderr()
        for stream in (managed.stdin, managed.stdout):
            if stream is not None:
                stream.close()


class _StagingHelper:
    """Controlled allocation for staging validation; START must remain forbidden."""

    def acquire(self) -> None:
        pass

    @property
    def allocation(self) -> SessionAllocation:
        return SessionAllocation("session", "/workspace")

    def start_process(
        self,
        process: ProcessSpec,
        listeners: Iterable[ListenerSpec],
        *,
        preferred_address: str | None = None,
    ) -> str:
        del process, listeners, preferred_address
        raise AssertionError("start_process() is not expected in this test")

    def observe_status(self) -> None:
        pass

    def live_processes(self) -> tuple[ManagedSshProcess, ...]:
        raise AssertionError("live_processes() is not expected in this test")

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        del timeout
        raise AssertionError("timeout_expired() is not expected in this test")

    def close(self) -> None:
        pass


def _assert_pidfd_exited(pidfd, timeout=5):
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    if not poller.poll(timeout * 1000):
        raise AssertionError("process survived cleanup")


def start_frame(
    argv,
    *,
    environment=None,
    required_paths=None,
    listeners=(),
    required_output_sentinels=(),
    readiness_timeout=30.0,
    literal_prefix=0,
    argv_templates=(),
    preferred_address=None,
    completion_policy="live_server",
):
    return encode_frame(
        "START",
        completion_policy=completion_policy,
        argv=list(argv),
        environment={} if environment is None else environment,
        required_paths=[] if required_paths is None else required_paths,
        services=list(listeners),
        required_output_sentinels=list(required_output_sentinels),
        readiness_timeout=readiness_timeout,
        literal_prefix=literal_prefix,
        argv_templates=list(argv_templates),
        preferred_address=preferred_address,
    )


def archive_bytes(members):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, member_type, linkname in members:
            info = tarfile.TarInfo(name)
            info.type = member_type
            if member_type == tarfile.SYMTYPE:
                info.linkname = linkname
            if member_type == tarfile.REGTYPE:
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            else:
                archive.addfile(info)
    return stream.getvalue()


class TestForwardingLifecycle:
    class Process:
        pid = os.getpid()

        def __init__(self, returncode=None):
            self.returncode = returncode
            self.terminate_calls = 0
            self.kill_calls = 0
            self.stdin = None
            self.stdout = None
            self.stderr = io.BytesIO(b"bind failed")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1
            self.returncode = 0

        def kill(self):
            self.kill_calls += 1
            self.returncode = -signal.SIGKILL

        def wait(self, timeout=None):
            return self.returncode

        def stderr_tail(self):
            return b""

        def close_stderr(self):
            self.stderr.close()

    class Command(_BlockedSshCommand):
        process: Any
        calls: list[tuple[str, str, SshForwardSpec | None]]

        def __init__(self, process):
            super().__init__()
            object.__setattr__(self, "process", process)
            object.__setattr__(self, "calls", [])

        def popen(self, host, remote_command, *, local_forward=None, on_acquired=None):
            self.calls.append((host, remote_command, local_forward))
            acquired_process = self.process
            if on_acquired is not None:
                on_acquired(acquired_process)
            return acquired_process

    @staticmethod
    def session(command):
        return SessionController(
            SessionPlan("target", command, TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )

    @staticmethod
    def port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def test_gdb_listener_readiness_does_not_probe_single_client_socket(
        self, requires_loopback_listener
    ):
        process = self.Process()
        command = self.Command(process)
        session = self.session(command)
        forward = ForwardSpec("gdb", self.port(), 3333)
        with (
            patch.object(forwarding_module, "await_ready", return_value=True),
            patch(
                "zephyr_remote_openocd.controller._forwarding.socket.create_connection"
            ) as connect,
        ):
            session._forwards.start((forward,), "127.64.1.1")
        connect.assert_not_called()
        remote_command = command.calls[0][1]
        assert remote_command.startswith("python3 -c ")
        assert command.calls[0][2] == SshForwardSpec(forward.local_port, "127.64.1.1", 3333)
        session._forwards.close()
        assert process.terminate_calls == 1

    def test_helper_control_connection_injects_no_optional_ssh_arguments(self, notified_authority):
        process = ControlProcess()
        command = self.Command(process)
        helper = _helper_client(
            SessionPlan("target", command, TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
            wiring=notified_authority,
        )
        try:
            helper.acquire()
        finally:
            helper.close()

        assert len(command.calls) == 1
        host, _remote_command, local_forward = command.calls[0]
        assert host == "target"
        assert local_forward is None

    def test_helper_startup_error_survives_process_cleanup_failure(self):
        process = ControlProcess(b"invalid helper frame\n")
        command = self.Command(process)

        with (
            patch.object(
                session_module,
                "deploy_helper",
                return_value=DeploymentResult("/helper.py", "digest", False),
            ),
            patch.object(
                connection_module,
                "stop_process",
                side_effect=RuntimeError("process cleanup failed"),
            ),
            pytest.raises(SessionError) as raised,
        ):
            session = SessionController.prepare(SessionPlan("target", command, TEST_PROCESS))
            with cleanup_on_exit(session.close):
                session.acquire()

        assert isinstance(raised.value.__cause__, ProtocolError)
        assert any("process cleanup failed" in note for note in raised.value.__notes__)

    def test_stale_gdb_forward_cannot_mask_current_forward_failure(self, monkeypatch):
        class ElapsedClock:
            """Forward startup observes a time whose readiness deadline has passed."""

            @staticmethod
            def monotonic() -> float:
                return time.monotonic() - forwarding_module.FORWARD_START_TIMEOUT - 1

        monkeypatch.setattr(forwarding_module, "time", ElapsedClock)
        stale = self.Process()
        read_fd, write_fd = os.pipe()
        stale.stdout = os.fdopen(read_fd, "rb")
        command = self.Command(stale)
        session = self.session(command)
        forward = ForwardSpec("gdb", 32155, 3333)
        monkeypatch.setattr(
            forwarding_module,
            "preflight",
            lambda _service: "stale listener",
        )
        try:
            with pytest.raises(SessionError) as raised:
                session._forwards.start((forward,), "127.64.1.1")
            message = str(raised.value)
            assert forward.name in message
            assert f"127.0.0.1:{forward.local_port}" in message
        finally:
            os.close(write_fd)
        session._forwards.close()
        assert stale.terminate_calls == 1

    def test_forward_manager_cleanup_attempts_all_forwards_once(self, monkeypatch):
        cleanup_error = RuntimeError("forward cleanup failed")
        terminate_calls = 0

        class Command(_BlockedSshCommand):
            processes: list[ManagedSshProcess]

            def __init__(self, processes: list[ManagedSshProcess]) -> None:
                super().__init__()
                object.__setattr__(self, "processes", processes)

            @override
            def popen(
                self,
                host: str,
                remote_command: str,
                *,
                local_forward: SshForwardSpec | None = None,
                on_acquired=None,
            ) -> ManagedSshProcess:
                del host, remote_command, local_forward
                acquired_process = self.processes.pop(0)
                if on_acquired is not None:
                    on_acquired(acquired_process)
                return acquired_process

        failed = local_transport(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        )
        healthy = local_transport(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
        )

        def fail_terminate() -> None:
            nonlocal terminate_calls
            terminate_calls += 1
            raise cleanup_error

        monkeypatch.setattr(failed, "terminate", fail_terminate)
        manager = ForwardManager(Command([failed, healthy]), "target")
        monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
        monkeypatch.setattr(forwarding_module, "await_ready", lambda *_args: True)
        try:
            manager.start(
                (ForwardSpec("gdb", 32155, 3333), ForwardSpec("tcl", 32156, 6666)),
                "127.64.1.1",
            )

            with pytest.raises(RuntimeError) as raised:
                manager.close()

            assert raised.value is cleanup_error
            assert healthy.poll() is not None
            assert healthy.stdin is not None and healthy.stdin.closed
            assert healthy.stdout is not None and healthy.stdout.closed
            assert not manager.has_forwards
            manager.close()
            assert terminate_calls == 1
        finally:
            for process in (failed, healthy):
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.close_stderr()
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()


@pytest.mark.parametrize(
    "phase", ("forward-entry", "helper-entry", "ownership-release", "between-forwards")
)
@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_repeated_ctrl_c_during_session_close_skips_no_cleanup(
    tmp_path, monkeypatch, phase, cleanup_fails
):
    processes: list[ManagedSshProcess] = []
    control_processes: list[ManagedSshProcess] = []
    helper_path = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
    environment = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)}
    initialize = subprocess.Popen.__init__
    previous_trace = sys.gettrace()
    cleanup_error = RuntimeError("transport stream cleanup failed")
    requested = False

    class LocalCommand(_BlockedSshCommand):
        @override
        def popen(self, host, remote_command, *, local_forward=None, on_acquired=None):
            process = SshCommand.popen(
                self, host, remote_command, local_forward=local_forward, on_acquired=on_acquired
            )
            owned = control_processes if local_forward is None else processes
            owned.append(process)
            return process

        @override
        def run_stream(self, host, remote_command, stream, *, timeout=60):
            return SshCommand.run_stream(self, host, remote_command, stream, timeout=timeout)

    def launch(process, argv, **kwargs):
        # Replace only SSH execution with local Python; the session, helper,
        # staging, readiness, and all transport cleanup use production code.
        remote_argv = shlex.split(argv[-1])
        assert remote_argv[0] == "python3"
        initialize(process, [sys.executable, "-u", *remote_argv[1:]], env=environment, **kwargs)

    session = SessionController(
        SessionPlan(
            "target",
            LocalCommand(),
            ProcessSpec(
                (
                    sys.executable,
                    "-c",
                    "import signal; print('child-ready', flush=True); signal.pause()",
                ),
                required_output_sentinels=("child-ready",),
            ),
            forwards=(ForwardSpec("gdb", 32155, 3333), ForwardSpec("tcl", 32156, 6666)),
        ),
        DeploymentResult(str(helper_path), "digest", False),
    )
    manager = session._forwards

    def interrupt_at_cleanup_boundary(frame: FrameType, event: str, _arg: object):
        nonlocal requested
        # Deliver real SIGINT at an ownership boundary without replacing
        # lifecycle collaborators or depending on thread scheduling/line numbers.
        cleanup = HelperConnection.close if phase == "helper-entry" else ForwardManager.close
        if event == "line" and frame.f_code is cleanup.__code__ and not requested:
            first = processes[0]
            second = processes[1]
            if phase in {"forward-entry", "helper-entry"}:
                at_boundary = True
            elif phase == "ownership-release":
                at_boundary = not manager.forwards and first.poll() is None
            else:
                at_boundary = (
                    first.stdin is not None
                    and first.stdin.closed
                    and first.stdout is not None
                    and first.stdout.closed
                    and second.poll() is None
                )
            if at_boundary:
                requested = True
                # Repeated Ctrl-C during settlement does not escalate.
                signal.raise_signal(signal.SIGINT)
                signal.raise_signal(signal.SIGINT)
        return interrupt_at_cleanup_boundary

    monkeypatch.setattr(subprocess.Popen, "__init__", launch)
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    close_failure: BaseException | None = None
    scope_outcome: BaseException | None = None
    try:
        with pytest.raises(OperationCancelled) as cancelled, operation_scope() as operation:
            session.acquire()
            assert session.descriptor is not None
            workspace = Path(session.descriptor.allocation.remote_workspace)
            if cleanup_fails:
                failing_process = control_processes[0] if phase == "helper-entry" else processes[0]
                close_stderr = failing_process.close_stderr

                def fail_close_stderr():
                    close_stderr()
                    raise cleanup_error

                monkeypatch.setattr(failing_process, "close_stderr", fail_close_stderr)

            sys.settrace(interrupt_at_cleanup_boundary)
            try:
                session.close()
            except RuntimeError as error:
                close_failure = error
            finally:
                sys.settrace(previous_trace)
            assert operation.requested
        scope_outcome = cancelled.value

        assert requested
        # Settlement was not interrupted: only real cleanup failures surface,
        # and the latched cancellation reaches the outer operation boundary.
        assert close_failure is (cleanup_error if cleanup_fails else None)
        assert isinstance(scope_outcome, OperationCancelled)
        for process in (*processes, *control_processes):
            assert process.poll() is not None
            assert process.stdin is not None and process.stdin.closed
            assert process.stdout is not None and process.stdout.closed
        assert session.closed
        assert not workspace.exists()
        assert not manager.forwards
        assert manager.forwards == ()
        session.close()
    finally:
        sys.settrace(previous_trace)
        if not session.closed:
            with suppress(RuntimeError):
                session.close()
        for process in (*processes, *control_processes):
            with suppress(RuntimeError):
                stop_process(process)


class TestRttClient:
    @staticmethod
    def _listener(handler):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        thread = threading.Thread(target=handler, args=(listener,), daemon=True)
        thread.start()
        return listener.getsockname()[1], thread

    def test_bidirectional_non_tty_channel(self, requires_loopback_listener, observed_session):
        expected_input = b"local_input"
        expected_output = b"remote_output"
        input_received = threading.Event()
        server_errors: list[BaseException] = []
        with ExitStack() as resources:
            listener = resources.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            stop_reader, stop_writer = socket.socketpair()
            resources.enter_context(stop_reader)
            resources.enter_context(stop_writer)
            stdin = resources.enter_context(tempfile.TemporaryFile("w+b"))
            stdout = resources.enter_context(io.BytesIO())
            stdin.write(expected_input)
            stdin.seek(0)

            def server():
                try:
                    readable, _, _ = select.select((listener, stop_reader), (), ())
                    if stop_reader in readable:
                        return
                    with listener.accept()[0] as connection:
                        connection.sendall(expected_output)
                        received = bytearray()
                        while len(received) < len(expected_input):
                            readable, _, _ = select.select((connection, stop_reader), (), ())
                            if stop_reader in readable:
                                return
                            payload = connection.recv(64)
                            assert payload, "RTT client closed before forwarding all input"
                            received.extend(payload)
                        assert received == expected_input
                        input_received.set()
                except BaseException as error:
                    server_errors.append(error)
                finally:
                    session.changed()

            thread = threading.Thread(target=server)

            def stop_server():
                try:
                    stop_writer.shutdown(socket.SHUT_WR)
                finally:
                    if thread.ident is not None:
                        thread.join(30)
                    assert not thread.is_alive(), "RTT test server did not stop"
                if server_errors:
                    raise server_errors[0]

            def poll_session():
                if server_errors:
                    raise server_errors[0]
                return (
                    0 if input_received.is_set() and stdout.getvalue() == expected_output else None
                )

            session = observed_session(poll_session)
            with cleanup_on_exit(stop_server):
                thread.start()
                assert (
                    run_rtt_client(listener.getsockname()[1], session, stdin=stdin, stdout=stdout)
                    == 0
                )
                assert input_received.is_set()
                assert stdout.getvalue() == expected_output

    def test_non_consuming_channel_does_not_block_session_poll(self, observed_session):
        local, peer = socket.socketpair()
        local.setblocking(False)
        # The peer never consumes input, so the channel stays unwritable.
        with suppress(BlockingIOError):
            while True:
                local.send(b"x" * 65536)
        input_read, input_write = os.pipe()
        os.write(input_write, b"pending input")
        os.close(input_write)
        finished = threading.Event()

        class Session(ObservedStatus):
            sleeps = 0

            @override
            def observation_sources(self) -> WaitSources:
                self.sleeps += 1
                if self.sleeps == 2:
                    # Input is pending for an unwritable channel; the session
                    # result must still end the relay.
                    finished.set()
                    self.changed()
                return super().observation_sources()

        session = Session(lambda: 0 if finished.is_set() else None)
        try:
            with (
                local,
                peer,
                os.fdopen(input_read, "rb", buffering=0) as stdin,
                tempfile.TemporaryFile("w+b") as stdout,
            ):
                relay = rtt_module.RttRelay(local, stdin.fileno(), stdout, session)
                assert relay.run() == 0
                assert relay.pending_input == b"pending input"
        finally:
            session.close()

    def test_full_input_queue_pauses_and_resumes_stdin_after_partial_send(
        self, monkeypatch, observed_session
    ):
        sent: list[bytes] = []
        received = bytearray()

        class PartialChannel(socket.socket):
            @override
            def send(self, data, flags=0):
                sent.append(bytes(data))
                return super().send(bytes(data[:2]), flags)

        monkeypatch.setattr(rtt_module, "_INPUT_CHUNK_SIZE", 4)
        monkeypatch.setattr(rtt_module, "_MAX_PENDING_INPUT", 4)
        local, peer = socket.socketpair()
        channel = PartialChannel(fileno=local.detach())
        channel.setblocking(False)
        input_read, input_write = os.pipe()
        os.write(input_write, b"abcdefgh")
        os.close(input_write)
        session = observed_session(lambda: 0 if received == b"abcdefgh" else None)

        def receive():
            while len(received) < 8:
                received.extend(peer.recv(64))
            session.changed()

        receiver = threading.Thread(target=receive)
        with channel, peer, os.fdopen(input_read, "rb", buffering=0) as stdin:
            receiver.start()
            try:
                with tempfile.TemporaryFile("w+b") as stdout:
                    assert rtt_module.RttRelay(channel, stdin.fileno(), stdout, session).run() == 0
            finally:
                receiver.join(30)

        # The full queue paused stdin; a partial send resumed it before more input.
        assert sent[:2] == [b"abcd", b"cdef"]
        assert received == b"abcdefgh"

    def test_rtt_eof_accounts_recorded_terminal_status(self, notified_authority):
        authority = notified_authority.authority

        class TerminalBeforeEof(socket.socket):
            @override
            def recv(self, bufsize, flags=0):
                authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))
                return super().recv(bufsize, flags)

        class Session:
            poll_operation = authority.operation_status

            @staticmethod
            def observation_sources() -> WaitSources:
                return WaitSources(notifiers=(notified_authority.notifier,))

        local, peer = socket.socketpair()
        connection = TerminalBeforeEof(fileno=local.detach())
        connection.setblocking(False)
        peer.close()
        with connection, tempfile.TemporaryFile("w+b") as stream:
            assert rtt_module.RttRelay(connection, stream.fileno(), stream, Session()).run() == 0

    def test_immediate_forwarded_channel_failure_is_authoritative(
        self, requires_loopback_listener, observed_session
    ):
        def server(listener):
            with listener, listener.accept()[0]:
                pass

        port, thread = self._listener(server)
        with pytest.raises(RttClientError):
            run_rtt_client(port, observed_session(lambda: None), startup_timeout=1)
        thread.join(2)

    def test_tty_preserves_signals_and_restores_complete_state(
        self, requires_loopback_listener, observed_session
    ):
        request = b"tty input"
        received = bytearray()
        input_received = threading.Event()
        input_sent = False

        def server(listener):
            with listener, listener.accept()[0] as connection:
                connection.sendall(b"connected")
                while len(received) < len(request):
                    payload = connection.recv(64)
                    if not payload:
                        return
                    received.extend(payload)
                input_received.set()
                session.changed()

        master_fd, slave_fd = os.openpty()
        try:
            with os.fdopen(slave_fd, "rb", buffering=0) as stdin, io.BytesIO() as stdout:
                original = termios.tcgetattr(stdin.fileno())
                original[3] |= termios.ICANON | termios.ECHO | termios.ISIG
                termios.tcsetattr(stdin.fileno(), termios.TCSANOW, original)
                port, thread = self._listener(server)

                def poll_session():
                    nonlocal input_sent
                    if not input_sent:
                        configured = termios.tcgetattr(stdin.fileno())
                        assert not configured[3] & termios.ICANON
                        assert not configured[3] & termios.ECHO
                        assert configured[3] & termios.ISIG
                        # Write after terminal setup (which flushes pending input).
                        # No newline: successful relay requires noncanonical mode.
                        os.write(master_fd, request)
                        input_sent = True
                    return 0 if input_received.is_set() else None

                session = observed_session(poll_session)
                try:
                    assert run_rtt_client(port, session, stdin=stdin, stdout=stdout) == 0
                    assert termios.tcgetattr(stdin.fileno()) == original
                finally:
                    thread.join(5)
                assert not thread.is_alive()
                assert received == request
        finally:
            os.close(master_fd)


@pytest.fixture
def helper_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    _session_id, workspace, owner_lock = workspace_module.new_workspace()
    try:
        yield workspace
    finally:
        try:
            workspace_module.remove_workspace(workspace)
        finally:
            owner_lock.close()


class TestRealProcessHelper:
    def test_helper_materializes_only_explicit_runner_owned_arguments(self, tmp_path):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        runtime = tmp_path / "runtime {workspace} {address}"
        runtime.mkdir()
        literal_file = tmp_path / "literal {workspace} {address}"
        literal_file.write_text("required literal file\n")
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(runtime)
        with subprocess.Popen(
            [sys.executable, str(helper), "control"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                assert process.stdin is not None and process.stdout is not None
                created = json.loads(read_line(process.stdout))
                workspace = Path(created["remote_workspace"])
                process.stdin.write(
                    start_frame(
                        [
                            sys.executable,
                            "-c",
                            "import json,sys; print(json.dumps(sys.argv[1:]))",
                            "fixed {workspace} {address}",
                            "",
                            "{workspace}/staged/file with spaces",
                            "{address}",
                            "puts {workspace} {address}",
                            "generated Tcl preview",
                        ],
                        literal_prefix=5,
                        required_paths=[
                            {"kind": "file", "path": str(literal_file)},
                            {
                                "kind": "directory",
                                "path": {"parts": [{"session": "workspace"}, "/staged"]},
                            },
                        ],
                        argv_templates=[
                            {
                                "index": 5,
                                "parts": [{"session": "workspace"}, "/staged/file with spaces"],
                            },
                            {"index": 6, "parts": [{"session": "address"}]},
                            {
                                "index": 8,
                                "parts": [
                                    "load_image ",
                                    {
                                        "tcl_word": [
                                            {"session": "workspace"},
                                            "/firmware {workspace} {address}",
                                        ]
                                    },
                                ],
                            },
                        ],
                    )
                )
                process.stdin.flush()
                events = [json.loads(line) for line in read_lines(process.stdout)]
                ready = next(event for event in events if event["type"] == "READY")
                assert events[0]["type"] == "ATTEMPT"
                output = "".join(
                    event["payload"] for event in events if event["type"] == "CHILD_OUTPUT"
                )
                assert json.loads(output) == [
                    "fixed {workspace} {address}",
                    "",
                    str(workspace / "staged/file with spaces"),
                    ready["remote_address"],
                    "puts {workspace} {address}",
                    'load_image "'
                    + str(workspace).replace("{", r"\{").replace("}", r"\}")
                    + r'/firmware \{workspace\} \{address\}"',
                ]
                assert events[0]["argv"] == [
                    sys.executable,
                    "-c",
                    "import json,sys; print(json.dumps(sys.argv[1:]))",
                    *json.loads(output),
                ]
                assert process.wait(timeout=10) == 0
                assert not workspace.exists()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)

    @pytest.mark.parametrize("pause", ("upload", "admission", "extraction"))
    def test_staging_and_cleanup_share_workspace_lifetime(self, tmp_path, pause):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(tmp_path)
        # Hooks only pause real I/O boundaries; both processes run the real helper.
        fixture = ROOT / "tests/fixtures/staging_lifetime.py"
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as contents:
            member = tarfile.TarInfo("nested/payload")
            member.size = 1
            contents.addfile(member, io.BytesIO(b"x"))
        release_read, release_write = os.pipe()
        processes = []
        try:
            control = subprocess.Popen(
                [sys.executable, str(fixture), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            processes.append(control)
            assert control.stdout is not None and control.stdin is not None
            assert control.stderr is not None
            workspace = Path(json.loads(read_line(control.stdout))["remote_workspace"])
            staging = subprocess.Popen(
                [sys.executable, str(fixture), "stage", str(workspace), pause, str(release_read)],
                env=environment,
                pass_fds=(release_read,),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            processes.append(staging)
            assert staging.stdin is not None and staging.stderr is not None
            staging.stdin.write(archive.getvalue())
            staging.stdin.close()
            assert read_line(staging.stderr).strip() == b"PAUSED"
            control.stdin.close()
            if pause != "extraction":
                assert control.wait(timeout=30) == 0
                assert tuple(workspace.parent.iterdir()) == ()
            else:
                assert read_line(control.stderr).strip() == b"CLEANUP"
                assert workspace.is_dir()
                # Admission is closed even though the existing stage still owns a lease.
                late = subprocess.run(
                    [sys.executable, str(helper), "stage", str(workspace)],
                    env=environment,
                    input=archive.getvalue(),
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                assert late.returncode != 0
                assert b'STAGED' not in late.stdout
            os.write(release_write, b"x")
            assert staging.wait(timeout=30) == (0 if pause == "extraction" else 1)
            assert control.wait(timeout=30) == 0
            assert staging.stdout is not None
            output = staging.stdout.read()
            assert (b'STAGED' in output) == (pause == "extraction")
            assert tuple(workspace.parent.iterdir()) == ()
        finally:
            os.close(release_read)
            os.close(release_write)
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=30)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    @pytest.mark.parametrize("file_mode, expected_mode", ((0o7777, 0o700), (0o044, 0o600)))
    def test_helper_normalizes_permissions_for_cleanup(
        self, helper_workspace, file_mode, expected_mode
    ):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        archive_stream = io.BytesIO()
        with tarfile.open(fileobj=archive_stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            payload = tarfile.TarInfo("restricted/payload")
            payload.mode = file_mode
            payload.size = 1
            archive.addfile(payload, io.BytesIO(b"x"))
            restricted = tarfile.TarInfo("restricted")
            restricted.type = tarfile.DIRTYPE
            restricted.mode = 0o500
            archive.addfile(restricted)

        workspace = helper_workspace
        environment = os.environ.copy()
        result = subprocess.run(
            [sys.executable, str(helper), "stage", str(workspace)],
            env=environment,
            input=archive_stream.getvalue(),
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        staged_directory = workspace / "staged" / "restricted"
        assert staged_directory.stat().st_mode & 0o777 == 0o700
        assert (staged_directory / "payload").stat().st_mode & 0o7777 == expected_mode
        shutil.rmtree(workspace)

    def test_helper_stages_large_archive_through_spooled_stdin(self, tmp_path, helper_workspace):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        payload = bytes(range(256)) * 8192
        source = tmp_path / "firmware.bin"
        source.write_bytes(payload)
        archive = build_archive((StagedFile(source, PurePosixPath("firmware.bin")),))

        workspace = helper_workspace
        environment = os.environ.copy()
        try:
            result = subprocess.run(
                [sys.executable, str(helper), "stage", str(workspace)],
                env=environment,
                stdin=archive.stream,
                capture_output=True,
                check=False,
            )
        finally:
            archive.stream.close()

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        assert json.loads(result.stdout) == {
            "version": 1,
            "type": "STAGED",
            "byte_count": archive.byte_count,
            "sha256": archive.sha256,
            "files": ["firmware.bin"],
            "directories": [],
        }
        assert (workspace / "staged" / "firmware.bin").read_bytes() == payload

    def test_helper_stages_empty_search_root(self, tmp_path, helper_workspace):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        local_root = tmp_path / "empty-search"
        local_root.mkdir()
        planner = PathPlanner(())
        planner.plan_directory(local_root, "search_0")
        archive = build_archive(planner.staged_files)

        workspace = helper_workspace
        environment = os.environ.copy()
        try:
            result = subprocess.run(
                [sys.executable, str(helper), "stage", str(workspace)],
                env=environment,
                input=archive.stream.read(),
                capture_output=True,
                check=False,
            )
        finally:
            archive.stream.close()

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        response = json.loads(result.stdout)
        assert response["files"] == []
        assert response["directories"] == ["trees/search_0"]
        assert (workspace / "staged" / "trees" / "search_0").is_dir()

    def test_helper_stages_empty_and_nested_directories(self, tmp_path, helper_workspace):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        local_root = tmp_path / "search"
        (local_root / "empty").mkdir(parents=True)
        (local_root / "nested" / "also-empty").mkdir(parents=True)
        (local_root / "nested" / "payload.cfg").write_text("payload")
        planner = PathPlanner(())
        planned = planner.plan_directory(local_root, "search_0")
        assert planned.remote == "{workspace}/staged/trees/search_0"
        archive = build_archive(planner.staged_files)

        workspace = helper_workspace
        environment = os.environ.copy()
        try:
            result = subprocess.run(
                [sys.executable, str(helper), "stage", str(workspace)],
                env=environment,
                input=archive.stream.read(),
                capture_output=True,
                check=False,
            )
        finally:
            archive.stream.close()

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        response = json.loads(result.stdout)
        assert response["byte_count"] == archive.byte_count == len(b"payload")
        assert response["sha256"] == archive.sha256 == hashlib.sha256(b"payload").hexdigest()
        assert response["files"] == ["trees/search_0/nested/payload.cfg"]
        assert response["directories"] == [
            "trees/search_0",
            "trees/search_0/empty",
            "trees/search_0/nested",
            "trees/search_0/nested/also-empty",
        ]
        staged = workspace / "staged"
        assert {path.relative_to(staged).as_posix() for path in staged.rglob("*")} == {
            "trees",
            "trees/search_0",
            "trees/search_0/empty",
            "trees/search_0/nested",
            "trees/search_0/nested/also-empty",
            "trees/search_0/nested/payload.cfg",
        }
        assert (staged / "trees/search_0/nested/payload.cfg").read_bytes() == b"payload"

    def test_helper_stages_multiple_files_with_interspersed_directories(
        self, tmp_path, helper_workspace
    ):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        tree = tmp_path / "tree"
        nested = tree / "nested directory"
        nested.mkdir(parents=True)
        first = tree / "z first.bin"
        second = nested / "a second.bin"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        archive = build_archive(
            (
                StagedDirectory(tree, PurePosixPath("trees/root")),
                StagedFile(first, PurePosixPath("trees/root/z first.bin")),
                StagedDirectory(nested, PurePosixPath("trees/root/nested directory")),
                StagedFile(second, PurePosixPath("trees/root/nested directory/a second.bin")),
            )
        )

        workspace = helper_workspace
        environment = os.environ.copy()
        try:
            result = subprocess.run(
                [sys.executable, str(helper), "stage", str(workspace)],
                env=environment,
                stdin=archive.stream,
                capture_output=True,
                check=False,
            )
        finally:
            archive.stream.close()

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        response = json.loads(result.stdout)
        assert response == {
            "version": 1,
            "type": "STAGED",
            "byte_count": len(b"firstsecond"),
            "sha256": hashlib.sha256(b"firstsecond").hexdigest(),
            "files": ["trees/root/z first.bin", "trees/root/nested directory/a second.bin"],
            "directories": ["trees/root", "trees/root/nested directory"],
        }
        assert archive.byte_count == response["byte_count"]
        assert archive.sha256 == response["sha256"]
        assert list(archive.files) == response["files"]
        assert list(archive.directories) == response["directories"]
        staged = workspace / "staged"
        assert (staged / "trees/root/z first.bin").read_bytes() == b"first"
        assert (staged / "trees/root/nested directory/a second.bin").read_bytes() == b"second"

    @pytest.mark.parametrize(
        "members",
        (
            (("../escape", tarfile.REGTYPE, None),),
            (("/absolute", tarfile.DIRTYPE, None),),
            (("../directory", tarfile.DIRTYPE, None),),
            (("nested/link", tarfile.SYMTYPE, "target"),),
            (("fifo", tarfile.FIFOTYPE, None),),
            (
                ("duplicate", tarfile.REGTYPE, None),
                ("duplicate", tarfile.REGTYPE, None),
            ),
            (
                ("file", tarfile.REGTYPE, None),
                ("file/child", tarfile.DIRTYPE, None),
            ),
            (
                ("directory", tarfile.REGTYPE, None),
                ("directory", tarfile.DIRTYPE, None),
            ),
        ),
    )
    def test_helper_rejects_unsafe_staging_members(self, helper_workspace, members):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        workspace = helper_workspace
        environment = os.environ.copy()

        result = subprocess.run(
            [sys.executable, str(helper), "stage", str(workspace)],
            env=environment,
            input=archive_bytes((("valid/payload", tarfile.REGTYPE, None), *members)),
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert json.loads(result.stdout)["type"] == "ERROR"
        assert tuple((workspace / "staged").iterdir()) == ()

    @pytest.mark.parametrize(
        "response",
        (
            encode_frame(
                "STAGED",
                byte_count=999,
                sha256=hashlib.sha256(b"firmware").hexdigest(),
                files=["firmware.bin"],
                directories=[],
            ),
            json.dumps(
                {
                    "version": 1,
                    "byte_count": len(b"firmware"),
                    "sha256": hashlib.sha256(b"firmware").hexdigest(),
                    "files": ["firmware.bin"],
                    "directories": [],
                }
            ).encode("utf-8")
            + b"\n",
            b"\xff",
        ),
        ids=("wrong-byte-count", "missing-type", "invalid-utf8"),
    )
    def test_controller_rejects_invalid_staging_confirmation(self, monkeypatch, response):
        class LocalCommand(_BlockedSshCommand):
            def run_stream(self, host, command, stream, timeout=60):
                stream.read()
                return subprocess.CompletedProcess(command, 0, response, b"")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "firmware.bin"
            source.write_bytes(b"firmware")
            session = SessionController(
                SessionPlan(
                    "local",
                    LocalCommand(),
                    TEST_PROCESS,
                    staged_files=(StagedFile(source, PurePosixPath("firmware.bin")),),
                ),
                DeploymentResult("/helper.py", "digest", False),
            )
            monkeypatch.setattr(
                session_module, "HelperConnection", lambda *_args, **_kwargs: _StagingHelper()
            )
            with (
                pytest.raises(SessionError, match="invalid remote staging response"),
                cleanup_on_exit(session.close),
            ):
                session.acquire()

    def test_controller_rejects_staging_confirmation_with_wrong_digest(self, monkeypatch, tmp_path):
        different_payload_digest = hashlib.sha256(b"different firmware").hexdigest()
        response = encode_frame(
            "STAGED",
            byte_count=len(b"firmware"),
            sha256=different_payload_digest,
            files=["firmware.bin"],
            directories=[],
        )

        class LocalCommand(_BlockedSshCommand):
            def run_stream(self, host, command, stream, timeout=60):
                stream.read()
                return subprocess.CompletedProcess(command, 0, response, b"")

        source = tmp_path / "firmware.bin"
        source.write_bytes(b"firmware")
        session = SessionController(
            SessionPlan(
                "local",
                LocalCommand(),
                TEST_PROCESS,
                staged_files=(StagedFile(source, PurePosixPath("firmware.bin")),),
            ),
            DeploymentResult("/helper.py", "digest", False),
        )
        monkeypatch.setattr(
            session_module, "HelperConnection", lambda *_args, **_kwargs: _StagingHelper()
        )

        with pytest.raises(SessionError), cleanup_on_exit(session.close):
            session.acquire()

    def test_controller_reports_nonzero_staging_command_and_closes_archive(self, monkeypatch):
        diagnostic = "staging destination is unavailable"
        staging_ssh_exit_status = 23
        response = encode_frame("ERROR", code="HELPER_ERROR", message=diagnostic)

        class LocalCommand(_BlockedSshCommand):
            stream: BinaryIO | None = None

            def run_stream(self, host, command, stream, timeout=60):
                self.stream = stream
                assert stream.read()
                return subprocess.CompletedProcess(command, staging_ssh_exit_status, response, b"")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "firmware.bin"
            source.write_bytes(b"firmware")
            command = LocalCommand()
            session = SessionController(
                SessionPlan(
                    "local",
                    command,
                    TEST_PROCESS,
                    staged_files=(StagedFile(source, PurePosixPath("firmware.bin")),),
                ),
                DeploymentResult("/helper.py", "digest", False),
            )
            monkeypatch.setattr(
                session_module, "HelperConnection", lambda *_args, **_kwargs: _StagingHelper()
            )

            with pytest.raises(SessionError) as raised, cleanup_on_exit(session.close):
                session.acquire()

            message = str(raised.value)
            assert str(staging_ssh_exit_status) in message
            assert "HELPER_ERROR" in message
            assert diagnostic in message
            assert command.stream is not None and command.stream.closed

    def test_controller_wraps_invalid_utf8_version_response(self, monkeypatch):
        class LocalCommand(_BlockedSshCommand):
            def run(
                self,
                host: str,
                command: str,
                /,
                *,
                input_data: bytes | None = None,
                timeout: float = 15,
            ) -> subprocess.CompletedProcess[bytes]:
                assert input_data is None
                return subprocess.CompletedProcess(command, 0, b"\xff", b"")

        monkeypatch.setattr(
            "zephyr_remote_openocd.controller.session.deploy_helper",
            lambda _command, _host: DeploymentResult("/helper.py", "digest", False),
        )

        with pytest.raises(SessionError, match="invalid remote OpenOCD version response"):
            query_remote_openocd_version(LocalCommand(), "local", ("openocd",))

    def test_helper_applies_requested_environment_before_child_executes(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdin is not None and process.stdout is not None
                read_line(process.stdout)
                process.stdin.write(
                    start_frame(
                        [sys.executable, "-c", "import os; print(os.environ['ZRO_TEST_FORWARD'])"],
                        environment={"ZRO_TEST_FORWARD": "before_config"},
                        completion_policy="process_exit",
                    )
                )
                process.stdin.flush()
                events = [json.loads(line) for line in read_lines(process.stdout)]
                output = next(event for event in events if event["type"] == "CHILD_OUTPUT")
                assert output == {
                    "version": 1,
                    "type": "CHILD_OUTPUT",
                    "stream": "stdout",
                    "payload": "before_config",
                    "line_end": True,
                }
                assert process.wait(timeout=5) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_helper_preserves_inherited_environment_when_request_omits_name(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            environment["ZRO_TEST_REMOTE_INHERITED"] = "remote-value"
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdin is not None and process.stdout is not None
                read_line(process.stdout)
                process.stdin.write(
                    start_frame(
                        [
                            sys.executable,
                            "-c",
                            "import os; print(os.environ['ZRO_TEST_REMOTE_INHERITED'])",
                        ],
                        completion_policy="process_exit",
                    )
                )
                process.stdin.flush()
                events = [json.loads(line) for line in read_lines(process.stdout)]
                output = next(event for event in events if event["type"] == "CHILD_OUTPUT")
                assert output["payload"] == "remote-value"
                assert process.wait(timeout=5) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    @pytest.mark.parametrize(
        ("frame", "close_input"),
        (
            pytest.param(b"not-json\n", False, id="malformed-json"),
            pytest.param(
                b'{"version":1,"type":"UNEXPECTED"}\n',
                False,
                id="unsupported-command",
            ),
            pytest.param(
                b'{"version":1,"type":"UNEXPECTED"}',
                True,
                id="missing-lf",
            ),
        ),
    )
    def test_helper_rejects_invalid_command_frame(self, frame, close_input):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdin is not None and process.stdout is not None
                read_line(process.stdout)
                process.stdin.write(frame)
                process.stdin.flush()
                if close_input:
                    process.stdin.close()
                error = json.loads(read_line(process.stdout))
                assert error["type"] == "SESSION_ENDED"
                assert error["trigger"] == "protocol_failure"
                assert process.wait(timeout=5) == 1
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_persistent_process_waits_for_sentinels_without_service_probes(
        self, requires_loopback_listener
    ):
        with socket.socket() as probe:
            probe.bind(("127.64.0.1", 0))
            remote_port = probe.getsockname()[1]
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            child_pidfd = None
            try:
                assert process.stdout is not None and process.stdin is not None
                created = json.loads(read_line(process.stdout))
                assert created["type"] == "SESSION_CREATED"
                workspace = Path(created["remote_workspace"])
                first_sentinel = "ZRO_INIT_READY_unit"
                second_sentinel = "ZRO_START_READY_unit"
                child_code = (
                    "import sys,time;"
                    "print(sys.argv[1],flush=True);"
                    "print(sys.argv[2],flush=True);"
                    "time.sleep(30)"
                )
                process.stdin.write(
                    start_frame(
                        [
                            sys.executable,
                            "-c",
                            child_code,
                            first_sentinel,
                            second_sentinel,
                        ],
                        listeners=[{"name": "tcl", "remote_port": remote_port}],
                        required_output_sentinels=(first_sentinel, second_sentinel),
                        readiness_timeout=5,
                    )
                )
                process.stdin.flush()
                events: list[dict[str, Any]] = []
                while not any(event["type"] == "READY" for event in events):
                    events.append(json.loads(read_line(process.stdout)))
                ready = next(event for event in events if event["type"] == "READY")
                child_pid = ready["child_pid"]
                child_pidfd = os.pidfd_open(child_pid)
                assert any(
                    event["type"] == "CHILD_OUTPUT"
                    and event["payload"] in {first_sentinel, second_sentinel}
                    for event in events
                )
                output_payloads = {
                    event["payload"] for event in events if event["type"] == "CHILD_OUTPUT"
                }
                assert output_payloads >= {first_sentinel, second_sentinel}
                process.stdin.close()
                assert process.wait(timeout=8) == 0
                assert not workspace.exists()
                _assert_pidfd_exited(child_pidfd)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                if child_pidfd is not None:
                    os.close(child_pidfd)

    def test_helper_version_operation_is_structured(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        result = subprocess.run(
            [sys.executable, str(helper), "openocd-version", sys.executable],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        message = json.loads(result.stdout)
        assert message["type"] == "OPENOCD_VERSION"
        assert "Python" in message["output"]

    @pytest.mark.parametrize("failure", ("diagnostic", "tcp-bind"))
    def test_helper_does_not_repeat_spawned_startup_work(
        self, requires_loopback_listener, isolated_lease_addresses, failure
    ):
        preferred, _ = isolated_lease_addresses
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            state = Path(directory) / "attempt"
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                assert json.loads(read_line(process.stdout))["type"] == "SESSION_CREATED"
                with socket.socket() as port_socket:
                    port_socket.bind(("127.0.0.1", 0))
                    remote_port = port_socket.getsockname()[1]
                output_sentinel = "ZRO_SENTINEL_collision"
                child = ROOT / "tests/fixtures/bind_collision_child.py"
                process.stdin.write(
                    start_frame(
                        [
                            sys.executable,
                            str(child),
                            REMOTE_ADDRESS_PLACEHOLDER,
                            str(remote_port),
                            failure,
                        ],
                        listeners=[{"name": "tcl", "remote_port": remote_port}],
                        environment={"ZRO_COLLISION_STATE": str(state)},
                        required_output_sentinels=(output_sentinel,),
                        argv_templates=[{"index": 2, "parts": [{"session": "address"}]}],
                        preferred_address=preferred,
                    )
                )
                process.stdin.flush()
                events: list[dict[str, Any]] = []
                while not any(event["type"] == "SESSION_ENDED" for event in events):
                    events.append(json.loads(read_line(process.stdout)))
                assert any(
                    event["type"] == "CHILD_OUTPUT"
                    and "address already in use" in event["payload"].casefold()
                    for event in events
                )
                attempts = [event["argv"] for event in events if event["type"] == "ATTEMPT"]
                assert len(attempts) == 1
                assert state.read_text().splitlines() == ["startup work"]
                assert not any(event["type"] == "READY" for event in events)
                snapshot = decode_terminal(events[-1])
                assert snapshot.outcome.child_result is not None
                assert snapshot.outcome.child_result.returncode == (
                    7 if failure == "diagnostic" else 1
                )
                assert snapshot.cleanup.confirmed
                process.stdin.close()
                assert process.wait(timeout=8) == 1
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_output_exit_status_and_workspace_cleanup(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                created = json.loads(read_line(process.stdout))
                assert created["type"] == "SESSION_CREATED"
                command = [
                    sys.executable,
                    "-c",
                    f'import sys;print("out");print("err",file=sys.stderr);'
                    f"sys.exit({OPENOCD_FAILURE_RC})",
                ]
                process.stdin.write(start_frame(command, completion_policy="process_exit"))
                process.stdin.flush()
                events = [json.loads(line) for line in read_lines(process.stdout)]
                assert process.wait(timeout=5) == 1
                assert events[0]["type"] == "ATTEMPT"
                assert not any(event["type"] == "READY" for event in events)
                outputs = {
                    (event["stream"], event["payload"])
                    for event in events
                    if event["type"] == "CHILD_OUTPUT" and event["payload"]
                }
                assert outputs == {("stdout", "out"), ("stderr", "err")}
                exit_event = next(event for event in events if event["type"] == "SESSION_ENDED")
                assert exit_event["child_result"]["returncode"] == OPENOCD_FAILURE_RC
                assert exit_event["trigger"] == "child_exit"
                assert not Path(created["remote_workspace"]).exists()
                assert process.stderr is not None
                assert process.stderr.read() == b""
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    @pytest.mark.parametrize("shutdown", ("eof", "signal"))
    def test_helper_cleans_up_while_protocol_output_pipe_is_full(self, tmp_path, shutdown):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        child_pid_file = tmp_path / "child.pid"
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(tmp_path)
        process = subprocess.Popen(
            [sys.executable, str(helper), "control"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        child_pid = None
        try:
            assert process.stdout is not None and process.stdin is not None
            created = json.loads(read_line(process.stdout))
            capacity = fcntl.fcntl(process.stdout.fileno(), fcntl.F_GETPIPE_SZ)
            # Fill the protocol pipe without queuing a scheduling-dependent
            # multi-megabyte decoding backlog ahead of shutdown.
            output_bytes = 2 * capacity
            process.stdin.write(
                start_frame(
                    [
                        sys.executable,
                        "-c",
                        "import os,pathlib,signal,sys;"
                        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));"
                        "sys.stdout.buffer.write(b'x'*int(sys.argv[2]));"
                        "sys.stdout.flush();signal.pause()",
                        str(child_pid_file),
                        str(output_bytes),
                    ],
                    required_output_sentinels=("not-ready",),
                )
            )
            process.stdin.flush()
            assert json.loads(read_line(process.stdout))["type"] == "ATTEMPT"
            deadline = time.monotonic() + 30
            # Poll an explicit OS condition; expiry is only a deadlock backstop.
            while True:
                unread = array.array("i", [0])
                fcntl.ioctl(process.stdout.fileno(), termios.FIONREAD, unread, True)
                if unread[0] == capacity and child_pid_file.exists():
                    break
                assert time.monotonic() < deadline, "helper output pipe did not fill"
                time.sleep(0.01)
            child_pid = int(child_pid_file.read_text())
            descriptor = os.pidfd_open(child_pid)
            try:
                if shutdown == "eof":
                    process.stdin.close()
                else:
                    process.send_signal(signal.SIGTERM)
                process.wait(timeout=20)
                poller = select.poll()
                poller.register(descriptor, select.POLLIN)
                assert poller.poll(5000), "child survived session shutdown"
                assert not Path(created["remote_workspace"]).exists()
            finally:
                os.close(descriptor)
        finally:
            if child_pid is None and child_pid_file.exists():
                child_pid = int(child_pid_file.read_text())
            if child_pid is not None:
                with suppress(ProcessLookupError):
                    os.killpg(child_pid, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def test_helper_signal_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            child_pid = None
            child_pidfd = None
            workspace = None
            try:
                assert process.stdout is not None and process.stdin is not None
                created = json.loads(read_line(process.stdout))
                assert created["type"] == "SESSION_CREATED"
                workspace = Path(created["remote_workspace"])
                command = [sys.executable, "-c", "import time; time.sleep(30)"]
                process.stdin.write(start_frame(command))
                process.stdin.flush()
                assert json.loads(read_line(process.stdout))["type"] == "ATTEMPT"
                started = json.loads(read_line(process.stdout))
                assert started["type"] == "READY"
                child_pid = started["child_pid"]
                child_pidfd = os.pidfd_open(child_pid)
                # Restricted sandboxes may block asyncio's socketpair wakeup
                # write, leaving the queued signal callback pending. Keep the
                # real-process deadline and run this test where that wakeup is
                # permitted instead of masking the environment failure.
                process.terminate()
                assert process.wait(timeout=8) == 1
                terminal = json.loads(read_line(process.stdout))
                assert terminal["trigger"] == "signal"
                assert terminal["primary_failure"] is not None
                assert not workspace.exists()
                _assert_pidfd_exited(child_pidfd)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                if child_pidfd is not None:
                    os.close(child_pidfd)

    @pytest.mark.parametrize("close_input", (True, False), ids=("eof", "unexpected-input"))
    def test_pending_readiness_shutdown_cleans_child_and_workspace(self, tmp_path, close_input):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(tmp_path)
        process = subprocess.Popen(
            [sys.executable, str(helper), "control"],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        child_pid = None
        child_pidfd = None
        try:
            assert process.stdout is not None and process.stdin is not None
            created = json.loads(read_line(process.stdout))
            workspace = Path(created["remote_workspace"])
            process.stdin.write(
                start_frame(
                    [
                        sys.executable,
                        "-c",
                        "import os, signal; print(os.getpid(), flush=True); signal.pause()",
                    ],
                    required_output_sentinels=["not-ready"],
                )
            )
            process.stdin.flush()
            # Child output is a handshake that startup is pending, not a readiness sentinel.
            assert json.loads(read_line(process.stdout))["type"] == "ATTEMPT"
            output = json.loads(read_line(process.stdout))
            assert output["type"] == "CHILD_OUTPUT"
            child_pid = int(output["payload"])
            child_pidfd = os.pidfd_open(child_pid)
            if close_input:
                process.stdin.close()
            else:
                process.stdin.write(encode_frame("UNEXPECTED"))
                process.stdin.flush()

            events = [json.loads(line) for line in read_lines(process.stdout, timeout=10)]

            assert process.wait(timeout=10) == (0 if close_input else 1)
            assert not any(event["type"] == "READY" for event in events)
            assert events[-1]["type"] == "SESSION_ENDED"
            assert events[-1]["trigger"] == (
                "controller_eof" if close_input else "protocol_failure"
            )
            assert not workspace.exists()
            _assert_pidfd_exited(child_pidfd)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if child_pid is not None:
                with suppress(ProcessLookupError):
                    os.killpg(child_pid, signal.SIGKILL)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            if child_pidfd is not None:
                os.close(child_pidfd)

    def test_helper_eof_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            child_pidfd = None
            try:
                assert process.stdout is not None and process.stdin is not None
                created = json.loads(read_line(process.stdout))
                assert created["type"] == "SESSION_CREATED"
                workspace = Path(created["remote_workspace"])
                process.stdin.write(
                    start_frame([sys.executable, "-c", "import time; time.sleep(30)"])
                )
                process.stdin.flush()
                assert json.loads(read_line(process.stdout))["type"] == "ATTEMPT"
                started = json.loads(read_line(process.stdout))
                assert started["type"] == "READY"
                child_pid = started["child_pid"]
                child_pidfd = os.pidfd_open(child_pid)
                process.stdin.close()
                assert process.wait(timeout=8) == 0
                assert not workspace.exists()
                _assert_pidfd_exited(child_pidfd)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                if child_pidfd is not None:
                    os.close(child_pidfd)

    def test_partial_openocd_start_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                created = json.loads(read_line(process.stdout))
                assert created["type"] == "SESSION_CREATED"
                workspace = Path(created["remote_workspace"])
                first_sentinel = "ZRO_INIT_partial"
                missing_sentinel = "ZRO_START_partial"
                command = [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.argv[1], flush=True)",
                    first_sentinel,
                ]
                process.stdin.write(
                    start_frame(
                        command,
                        required_output_sentinels=(first_sentinel, missing_sentinel),
                    )
                )
                process.stdin.flush()
                events = [json.loads(line) for line in read_lines(process.stdout)]
                assert any(event["type"] == "CHILD_OUTPUT" for event in events)
                assert events[-1]["type"] == "SESSION_ENDED"
                assert process.wait(timeout=8) == 1
                assert not workspace.exists()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_controller_publishes_child_failure_and_relays_output(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand(_BlockedSshCommand):
                argv_prefix = ("local_test",)

                def popen(inner, host, remote_command, *, local_forward=None, on_acquired=None):  # pylint: disable=no-self-argument
                    return local_transport(
                        [sys.executable, str(helper), "control"],
                        environment=environment,
                        on_acquired=on_acquired,
                    )

                def run_stream(inner, host, remote_command, stream, timeout=60):  # pylint: disable=no-self-argument
                    workspace = remote_command.rsplit(" ", 1)[1]
                    return subprocess.run(
                        [sys.executable, str(helper), "stage", workspace],
                        env=environment,
                        input=stream.read(),
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )

            output = []
            sample_openocd_exit_code = 6
            remote_process = ProcessSpec(
                (
                    sys.executable,
                    "-c",
                    f'import sys;print("hello");sys.exit({sample_openocd_exit_code})',
                ),
                completion_policy=CompletionPolicy.PROCESS_EXIT,
            )
            request = SessionPlan("local", LocalCommand(), process=remote_process)
            deployment = DeploymentResult(str(helper), "digest", False)
            with patch.object(session_module, "deploy_helper", return_value=deployment):
                session_controller = SessionController.prepare(request)
            try:
                session_controller.acquire(
                    output_handler=lambda stream, payload, line_end: output.append(
                        (stream, payload, line_end)
                    ),
                )
                assert session_controller.descriptor is None
                with pytest.raises(SessionError, match="exit status 6") as initial:
                    session_controller.wait_for_openocd_exit(5)
                assert session_controller.openocd_returncode == sample_openocd_exit_code
                assert [
                    (payload, line_end)
                    for stream, payload, line_end in output
                    if stream == "stdout"
                ] == [("hello", True)]
                assert [
                    (payload, line_end)
                    for stream, payload, line_end in output
                    if stream == "stderr"
                ] == []
            finally:
                with pytest.raises(SessionError) as final:
                    session_controller.finish()
            assert final.value is initial.value
            assert any("status 1" in note for note in final.value.__notes__)
            assert session_controller.closed
            assert session_controller.openocd_returncode == sample_openocd_exit_code

    def test_controller_surfaces_helper_descendant_warning(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand(_BlockedSshCommand):
                argv_prefix = ("local_test",)

                def popen(inner, host, remote_command, *, local_forward=None, on_acquired=None):  # pylint: disable=no-self-argument
                    return local_transport(
                        [sys.executable, str(helper), "control"],
                        environment=environment,
                        on_acquired=on_acquired,
                    )

                def run_stream(inner, host, remote_command, stream, timeout=60):  # pylint: disable=no-self-argument
                    workspace = remote_command.rsplit(" ", 1)[1]
                    return subprocess.run(
                        [sys.executable, str(helper), "stage", workspace],
                        env=environment,
                        input=stream.read(),
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )

            child = ROOT / "tests/fixtures/group_descendant_child.py"
            remote_process = ProcessSpec(
                (sys.executable, str(child), "ZRO_DESCENDANT_READY"),
                required_output_sentinels=("ZRO_DESCENDANT_READY",),
            )
            output = []
            session_controller = SessionController(
                SessionPlan("local", LocalCommand(), process=remote_process),
                DeploymentResult(str(helper), "digest", False),
            )
            try:
                session_controller.acquire(
                    output_handler=lambda stream, payload, line_end: output.append(
                        (stream, payload, line_end)
                    )
                )
                session_controller.close()
            finally:
                with suppress(BaseException):
                    session_controller.close()

            warning = "warning: terminating remaining OpenOCD process-group members:"
            assert any(
                stream == "stderr" and warning in payload for stream, payload, _line_end in output
            )

    @pytest.mark.parametrize(
        ("event_stream_tail", "exit_code", "expected", "expected_openocd_result"),
        (
            (terminal_message().decode().rstrip("\n"), 0, None, None),
            (
                (
                    encode_frame("ATTEMPT", argv=["child"])
                    + terminal_message(Trigger.CHILD_EXIT, returncode=0)
                )
                .decode()
                .rstrip("\n"),
                0,
                None,
                0,
            ),
            (
                terminal_message().decode() + terminal_message().decode().rstrip("\n"),
                0,
                ("outside session lifetime",),
                None,
            ),
            (terminal_message().decode() + "not-json", 0, ("malformed",), None),
            (
                terminal_message(Trigger.HELPER_FAILURE, code="CLEANUP", message="cleanup failed")
                .decode()
                .rstrip("\n"),
                7,
                ("cleanup failed", "status 7"),
                None,
            ),
            (terminal_message().decode().rstrip("\n"), 7, ("status 7",), None),
            (None, 7, ("did not produce SESSION_ENDED",), None),
        ),
    )
    def test_helper_client_close_validates_helper_shutdown(
        self,
        notified_authority,
        event_stream_tail,
        exit_code,
        expected,
        expected_openocd_result,
    ):
        event_stream_tail = "" if event_stream_tail is None else event_stream_tail + "\n"
        helper_argv = [
            sys.executable,
            str(SCRIPTED_HELPER),
            str(exit_code),
            event_stream_tail,
            "any",
        ]

        class LocalCommand(_BlockedSshCommand):
            def popen(inner, host, remote_command, *, local_forward=None, on_acquired=None):  # pylint: disable=no-self-argument
                return local_transport(helper_argv, on_acquired=on_acquired)

        authority = notified_authority.authority
        helper_client = _helper_client(
            SessionPlan("local", LocalCommand(), TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
            wiring=notified_authority,
        )
        try:
            helper_client.acquire()
            if expected is None:
                helper_client.close()
                assert authority.snapshot().failure is None
                assert authority.operation_status() == expected_openocd_result
            else:
                helper_client.close()
                error = authority.snapshot().failure
                assert error is not None
                assert all(
                    fragment in str(error) + " ".join(getattr(error, "__notes__", ()))
                    for fragment in expected
                )
        finally:
            with suppress(BaseException):
                helper_client.close()

    def test_helper_client_reports_helper_failure_after_close_event(self, notified_authority):
        events = (
            encode_frame("ATTEMPT", argv=["test-process"])
            + encode_frame("READY", remote_address="127.64.0.1", child_pid=1)
            + terminal_message(Trigger.CHILD_EXIT, returncode=0)
        )
        helper_argv = [
            sys.executable,
            str(SCRIPTED_HELPER),
            str(HELPER_FAILURE_RC),
            events.decode(),
            "start",
        ]

        class LocalCommand(_BlockedSshCommand):
            process: Any = None

            def popen(inner, host, remote_command, *, local_forward=None, on_acquired=None):  # pylint: disable=no-self-argument
                process = local_transport(helper_argv, on_acquired=on_acquired)
                inner.process = process
                return process

        command = LocalCommand()
        authority = notified_authority.authority
        helper_client = _helper_client(
            SessionPlan("local", command, TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
            wiring=notified_authority,
        )
        helper_exited = threading.Event()
        drain_events = helper_client._drain_events

        def drain_after_exit():
            assert helper_exited.wait(5)
            drain_events()

        try:
            helper_client.acquire()
            with patch.object(helper_client, "_drain_events", side_effect=drain_after_exit):
                helper_client.start_process(TEST_PROCESS, ())
                command.process.wait(timeout=5)
                helper_exited.set()
                assert helper_client._join_reader(timeout=5)
            helper_client.observe_status()
            with pytest.raises(SessionError, match="status 9"):
                authority.operation_status()
            terminal = authority.snapshot().terminal
            assert terminal is not None
            child_result = terminal.outcome.child_result
            assert child_result is not None
            assert child_result.returncode == 0
        finally:
            helper_exited.set()
            with suppress(BaseException):
                helper_client.close()

    def test_helper_client_close_waits_for_child_kill_fallback(self, notified_authority):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            child_pid_path = Path(directory) / "child.pid"

            class LocalCommand(_BlockedSshCommand):
                argv_prefix = ("local_test",)
                process: Any = None

                def popen(inner, host, remote_command, *, local_forward=None, on_acquired=None):  # pylint: disable=no-self-argument
                    process = local_transport(
                        [sys.executable, str(helper), "control"],
                        environment=environment,
                        on_acquired=on_acquired,
                    )
                    inner.process = process
                    return process

            output_sentinel = "ZRO_SENTINEL_ignore_term"
            child_code = (
                "from pathlib import Path;"
                "import os,signal,sys,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii');"
                "print(sys.argv[1], flush=True);"
                "time.sleep(30)"
            )
            remote_process = ProcessSpec(
                (sys.executable, "-c", child_code, output_sentinel, str(child_pid_path)),
                required_output_sentinels=(output_sentinel,),
            )
            command = LocalCommand()
            authority = notified_authority.authority
            helper_client = _helper_client(
                SessionPlan("local", command, process=remote_process),
                DeploymentResult(str(helper), "digest", False),
                wiring=notified_authority,
            )
            child_pid = None
            child_pidfd = None
            try:
                helper_client.acquire()
                workspace = Path(helper_client.allocation.remote_workspace)
                helper_client.start_process(remote_process, ())
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                child_pidfd = os.pidfd_open(child_pid)

                helper_client.close()

                assert authority.snapshot().failure is None
                assert command.process.returncode == 0
                assert not workspace.exists()
                _assert_pidfd_exited(child_pidfd)
            finally:
                if child_pidfd is not None:
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(child_pidfd, signal.SIGKILL)
                    os.close(child_pidfd)

    def test_controller_reader_failure_requires_lifecycle_cleanup(self):
        helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand(_BlockedSshCommand):
                def popen(inner, host, remote_command, *, local_forward=None, on_acquired=None):  # pylint: disable=no-self-argument
                    return local_transport(
                        [sys.executable, str(helper), "control"],
                        environment=environment,
                        on_acquired=on_acquired,
                    )

                def run_stream(inner, host, remote_command, stream, timeout=60):  # pylint: disable=no-self-argument
                    workspace = remote_command.rsplit(" ", 1)[1]
                    return subprocess.run(
                        [sys.executable, str(helper), "stage", workspace],
                        env=environment,
                        input=stream.read(),
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )

            remote_process = ProcessSpec(
                (
                    sys.executable,
                    "-c",
                    'import sys,time;print("hello",flush=True);time.sleep(30)',
                ),
            )
            request = SessionPlan("local", LocalCommand(), process=remote_process)

            output_error = RuntimeError()

            def fail_on_output(_stream, _payload, _line_end):
                raise output_error

            session_controller = SessionController(
                request,
                DeploymentResult(str(helper), "digest", False),
            )
            try:
                with pytest.raises(SessionError) as raised:
                    # The reader publishes the failure at the next observation.
                    session_controller.acquire(output_handler=fail_on_output)
                    session_controller.wait_for_openocd_exit(5)
                assert raised.value.__cause__ is output_error
                assert not session_controller.closed
                with pytest.raises(SessionError) as raised:
                    session_controller.close()
                assert raised.value.__cause__ is output_error
            finally:
                with suppress(BaseException):
                    session_controller.close()


def _helper_client(request, deployment, output_handler=None, *, wiring: NotifiedAuthority):
    return HelperConnection(
        request.ssh_command,
        request.host,
        deployment,
        output_handler=output_handler,
        authority=wiring.authority,
        notifier=wiring.notifier,
    )
