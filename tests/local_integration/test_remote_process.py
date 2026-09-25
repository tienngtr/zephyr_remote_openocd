# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, override
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote import forwarding as forwarding_module
from zephyr_remote_openocd.remote import helper_client as helper_client_module
from zephyr_remote_openocd.remote import rtt as rtt_module
from zephyr_remote_openocd.remote.backend import (
    RemoteSession,
    query_remote_openocd_version,
)
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.forwarding import _ForwardManager
from zephyr_remote_openocd.remote.helper_client import _HelperClient, _HelperCloseResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    StagedDirectory,
    StagedFile,
)
from zephyr_remote_openocd.remote.paths import REMOTE_ADDRESS_PLACEHOLDER, PathPlanner
from zephyr_remote_openocd.remote.protocol import encode_message
from zephyr_remote_openocd.remote.rtt import RttClientError, run_rtt_client
from zephyr_remote_openocd.remote.services import (
    LOOPBACK_RANGE,
)
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, SshCommand
from zephyr_remote_openocd.remote.staging import build_archive

from tests.process_support import read_line, read_lines
from tests.support import ROOT

TEST_PROCESS = RemoteProcess(("test-process",))
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
    def popen(self, host: str, remote_command: str, *extra_args: str) -> ManagedSshProcess:
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


class _StagingHelper:
    """Strict helper fake exposing only the staging allocation."""

    @property
    def openocd_returncode(self) -> int | None:
        raise AssertionError("openocd_returncode is not expected in this test")

    @property
    def allocation(self) -> SessionAllocation:
        return SessionAllocation("session", "/workspace")

    def start_process(self, process: RemoteProcess, services: Iterable[Service]) -> str:
        del process, services
        raise AssertionError("start_process() is not expected in this test")

    def recorded_openocd_exit(self) -> int | None:
        raise AssertionError("recorded_openocd_exit() is not expected in this test")

    def wait_for_change(self, timeout: float | None) -> None:
        del timeout
        raise AssertionError("wait_for_change() is not expected in this test")

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        del timeout
        raise AssertionError("timeout_expired() is not expected in this test")

    def close(self) -> _HelperCloseResult:
        raise AssertionError("close() is not expected in this test")


def _assert_pidfd_exited(pidfd, timeout=5):
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    if not poller.poll(timeout * 1000):
        raise AssertionError("process survived cleanup")


def managed_popen(*args, **kwargs):
    return ManagedSshProcess.from_popen(subprocess.Popen(*args, **kwargs))


def start_frame(
    argv,
    *,
    environment=None,
    required_paths=None,
    services=(),
    required_output_sentinels=(),
    readiness_timeout=30.0,
    literal_prefix=0,
):
    return encode_message(
        "START",
        argv=list(argv),
        environment={} if environment is None else environment,
        required_paths=[] if required_paths is None else required_paths,
        services=list(services),
        required_output_sentinels=list(required_output_sentinels),
        readiness_timeout=readiness_timeout,
        literal_prefix=literal_prefix,
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
        calls: list[tuple[str, str, tuple[str, ...]]]

        def __init__(self, process):
            super().__init__()
            object.__setattr__(self, "process", process)
            object.__setattr__(self, "calls", [])

        def popen(self, host, remote_command, *extra_args):
            self.calls.append((host, remote_command, extra_args))
            return self.process

    @staticmethod
    def session(command):
        return RemoteSession(
            RemoteSessionRequest("target", command, TEST_PROCESS),
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
        service = Service("gdb", self.port(), 3333)
        with (
            patch.object(_ForwardManager, "_await_ready", return_value=True),
            patch("zephyr_remote_openocd.remote.forwarding.socket.create_connection") as connect,
        ):
            session._forwards.start((service,), "127.64.1.1")
        connect.assert_not_called()
        remote_command = command.calls[0][1]
        assert remote_command.startswith("python3 -c ")
        assert "-N" not in command.calls[0][2]
        session._forwards.close()
        assert process.terminate_calls == 1

    def test_helper_control_connection_injects_no_optional_ssh_arguments(self):
        process = self.Process()
        process.stdout = io.BytesIO()
        command = self.Command(process)
        events = (
            {
                "type": "SESSION_CREATED",
                "helper": "zephyr_remote_openocd",
                "session_id": "session",
                "remote_workspace": "/workspace",
            },
        )
        with patch.object(_HelperClient, "_read_event", side_effect=events):
            _opened_helper_client(
                RemoteSessionRequest("target", command, TEST_PROCESS),
                DeploymentResult("/helper.py", "digest", False),
            )

        assert len(command.calls) == 1
        host, _remote_command, extra_args = command.calls[0]
        assert host == "target"
        assert extra_args == ()

    def test_helper_startup_error_survives_process_cleanup_failure(self):
        process = self.Process()
        process.stdout = io.BytesIO()
        command = self.Command(process)
        startup_error = SessionError("invalid helper response")

        with (
            patch.object(_HelperClient, "_read_event", side_effect=startup_error),
            patch.object(
                helper_client_module,
                "_stop_process",
                side_effect=RuntimeError("process cleanup failed"),
            ),
            pytest.raises(SessionError) as raised,
        ):
            _opened_helper_client(
                RemoteSessionRequest("target", command, TEST_PROCESS),
                DeploymentResult("/helper.py", "digest", False),
            )

        assert raised.value is startup_error
        assert any(
            note.startswith("helper startup cleanup also failed:")
            for note in raised.value.__notes__
        )
        assert any("process cleanup failed" in note for note in raised.value.__notes__)

    def test_stale_gdb_forward_cannot_mask_current_forward_failure(self, monkeypatch):
        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

        class Selector:
            @staticmethod
            def register(_stream, _events):
                pass

            @staticmethod
            def select(_timeout):
                clock.now = forwarding_module.FORWARD_START_TIMEOUT + 1
                return []

            @staticmethod
            def close():
                pass

        clock = Clock()
        monkeypatch.setattr(forwarding_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(forwarding_module.selectors, "DefaultSelector", Selector)
        stale = self.Process()
        read_fd, write_fd = os.pipe()
        stale.stdout = os.fdopen(read_fd, "rb")
        command = self.Command(stale)
        session = self.session(command)
        service = Service("gdb", 32155, 3333)
        monkeypatch.setattr(
            _ForwardManager,
            "_preflight",
            staticmethod(lambda _service: "stale listener"),
        )
        try:
            with pytest.raises(SessionError) as raised:
                session._forwards.start((service,), "127.64.1.1")
            message = str(raised.value)
            assert service.name in message
            assert f"127.0.0.1:{service.local_port}" in message
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
            def popen(self, host: str, remote_command: str, *extra_args: str) -> ManagedSshProcess:
                del host, remote_command, extra_args
                return self.processes.pop(0)

        failed = managed_popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        healthy = managed_popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def fail_terminate() -> None:
            nonlocal terminate_calls
            terminate_calls += 1
            raise cleanup_error

        monkeypatch.setattr(failed, "terminate", fail_terminate)
        manager = _ForwardManager(Command([failed, healthy]), "target")
        monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
        monkeypatch.setattr(_ForwardManager, "_await_ready", staticmethod(lambda *_args: True))
        try:
            manager.start(
                (Service("gdb", 32155, 3333), Service("tcl", 32156, 6666)),
                "127.64.1.1",
            )

            with pytest.raises(RuntimeError) as raised:
                manager.close()

            assert raised.value is cleanup_error
            assert healthy.poll() is not None
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


class TestRttClient:
    @staticmethod
    def _listener(handler):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        thread = threading.Thread(target=handler, args=(listener,), daemon=True)
        thread.start()
        return listener.getsockname()[1], thread

    def test_bidirectional_non_tty_channel(self, requires_loopback_listener):
        received = []

        def server(listener):
            with listener, listener.accept()[0] as connection:
                connection.sendall(b"remote_output")
                received.append(connection.recv(64))

        port, thread = self._listener(server)
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        os.write(input_write, b"local_input")
        os.close(input_write)
        with (
            os.fdopen(input_read, "rb", buffering=0) as stdin,
            os.fdopen(output_write, "wb", buffering=0) as stdout,
        ):
            assert (
                run_rtt_client(
                    port,
                    lambda: 0 if received else None,
                    stdin=stdin,
                    stdout=stdout,
                )
                == 0
            )
        thread.join(2)
        assert received == [b"local_input"]
        assert os.read(output_read, 64) == b"remote_output"
        os.close(output_read)

    def test_non_consuming_channel_does_not_block_session_poll(self):
        class Connection:
            def sendall(self, _payload):
                raise AssertionError("blocking sendall must not be used")

            def send(self, _payload):
                raise AssertionError("socket was not writable")

            def close(self):
                pass

        connection = Connection()
        input_read, input_write = os.pipe()
        os.write(input_write, b"pending input")
        os.close(input_write)
        input_became_readable = False
        observed_after_input = False

        def poll_session():
            nonlocal observed_after_input
            if input_became_readable:
                observed_after_input = True
                return 0
            return None

        def select_input(_readable, _writable, _exceptional, _timeout):
            nonlocal input_became_readable
            input_became_readable = True
            return [stdin.fileno()], [], []

        with (
            os.fdopen(input_read, "rb", buffering=0) as stdin,
            tempfile.TemporaryFile("w+b") as stdout,
            patch.object(rtt_module, "_connect", return_value=(connection, b"")),
            patch.object(
                rtt_module.select,
                "select",
                side_effect=select_input,
            ) as select_call,
        ):
            input_fd = stdin.fileno()
            assert run_rtt_client(5555, poll_session, stdin=stdin, stdout=stdout) == 0

        assert observed_after_input
        select_call.assert_called_once_with((input_fd, connection), (), (), 0.1)

    def test_full_input_queue_pauses_and_resumes_stdin_after_partial_send(self, monkeypatch):
        input_resumed = False
        input_forwarded_after_resume = False
        select_requests = []
        input_was_paused = False

        class Connection:
            def __init__(self):
                self.sent = []

            def send(self, payload):
                nonlocal input_forwarded_after_resume
                self.sent.append(bytes(payload))
                if input_resumed:
                    input_forwarded_after_resume = True
                return 2

            def close(self):
                pass

        connection = Connection()
        monkeypatch.setattr(rtt_module, "_INPUT_CHUNK_SIZE", 4)
        monkeypatch.setattr(rtt_module, "_MAX_PENDING_INPUT", 4)
        input_read, input_write = os.pipe()
        os.write(input_write, b"abcdefgh")
        os.close(input_write)

        def poll_session():
            return 0 if input_forwarded_after_resume else None

        def select_io(_readable, _writable, _exceptional, _timeout):
            nonlocal input_resumed, input_was_paused
            select_requests.append((_readable, _writable))
            if input_fd not in _readable and connection in _writable:
                input_was_paused = True
            if input_was_paused and input_fd in _readable:
                input_resumed = True
            if not connection.sent:
                if _writable:
                    return [], [connection], []
                return [input_fd], [], []
            return [input_fd], [connection], []

        with (
            os.fdopen(input_read, "rb", buffering=0) as stdin,
            tempfile.TemporaryFile("w+b") as stdout,
        ):
            input_fd = stdin.fileno()
            with (
                patch.object(rtt_module, "_connect", return_value=(connection, b"")),
                patch.object(
                    rtt_module.select,
                    "select",
                    side_effect=select_io,
                ),
            ):
                assert run_rtt_client(5555, poll_session, stdin=stdin, stdout=stdout) == 0

        assert input_forwarded_after_resume
        assert any(input_fd in readable for readable, _ in select_requests)
        pause_index = next(
            index
            for index, (readable, writable) in enumerate(select_requests)
            if input_fd not in readable and connection in writable
        )
        assert any(input_fd in readable for readable, _ in select_requests[pause_index + 1 :])
        assert connection.sent == [b"abcd", b"cdef"]

    def test_established_channel_closure_fails_while_session_is_running(self):
        class Connection:
            def recv(self, _size):
                return b""

            def close(self):
                pass

        connection = Connection()
        with (
            tempfile.TemporaryFile("w+b") as stream,
            patch.object(rtt_module, "_connect", return_value=(connection, b"connected")),
            patch.object(rtt_module.select, "select", return_value=([connection], [], [])),
            pytest.raises(RttClientError),
        ):
            run_rtt_client(5555, lambda: None, stdin=stream, stdout=stream)

    def test_eof_drains_pending_session_closed_status(self):
        class Connection:
            def recv(self, _size):
                helper_client._dispatch(
                    {"type": "SESSION_CLOSED", "reason": "process_exit", "returncode": 0}
                )
                return b""

            def close(self):
                pass

        helper_client = _HelperClient(
            SshCommand(), "target", DeploymentResult("/helper.py", "digest", False)
        )
        connection = Connection()
        with (
            tempfile.TemporaryFile("w+b") as stream,
            patch.object(rtt_module, "_connect", return_value=(connection, b"connected")),
            patch.object(rtt_module.select, "select", return_value=([connection], [], [])),
        ):
            assert (
                run_rtt_client(
                    5555,
                    helper_client.recorded_openocd_exit,
                    stdin=stream,
                    stdout=stream,
                )
                == 0
            )

    def test_immediate_forwarded_channel_failure_is_authoritative(self, requires_loopback_listener):
        def server(listener):
            with listener, listener.accept()[0]:
                pass

        port, thread = self._listener(server)
        with pytest.raises(RttClientError):
            run_rtt_client(port, lambda: None, startup_timeout=1)
        thread.join(2)

    def test_tty_preserves_signals_and_restores_complete_state(self):
        input_closed = False
        channel_closed = False

        class Connection:
            def recv(self, _size):
                nonlocal channel_closed
                channel_closed = True
                return b""

            def close(self):
                pass

        def read_input(_fd, _size):
            nonlocal input_closed
            input_closed = True
            return b""

        def select_io(_readable, _writable, _exceptional, _timeout):
            if input_closed:
                return [connection], [], []
            return [stream.fileno()], [], []

        original = [
            1,
            2,
            3,
            rtt_module.termios.ICANON | rtt_module.termios.ECHO | rtt_module.termios.ISIG,
            5,
            6,
            [7],
        ]
        connection = Connection()
        with (
            tempfile.TemporaryFile("w+b") as stream,
            patch.object(rtt_module, "_connect", return_value=(connection, b"")),
            patch.object(rtt_module.os, "isatty", return_value=True),
            patch.object(rtt_module.os, "read", side_effect=read_input),
            patch.object(
                rtt_module.select,
                "select",
                side_effect=select_io,
            ),
            patch.object(
                rtt_module.termios,
                "tcgetattr",
                side_effect=[list(original), list(original)],
            ),
            patch.object(rtt_module.termios, "tcsetattr") as set_attributes,
        ):
            assert (
                run_rtt_client(
                    5555,
                    lambda: 0 if channel_closed else None,
                    stdin=stream,
                    stdout=stream,
                )
                == 0
            )
        configured = set_attributes.call_args_list[0].args[2]
        assert not configured[3] & rtt_module.termios.ICANON
        assert not configured[3] & rtt_module.termios.ECHO
        assert configured[3] & rtt_module.termios.ISIG
        assert set_attributes.call_args_list[-1].args[2] == original


class TestRealProcessHelper:
    def test_helper_normalizes_restricted_directory_for_cleanup(self, tmp_path):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        archive_stream = io.BytesIO()
        with tarfile.open(fileobj=archive_stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            payload = tarfile.TarInfo("restricted/payload")
            payload.size = 1
            payload.mode = 0o600
            archive.addfile(payload, io.BytesIO(b"x"))
            restricted = tarfile.TarInfo("restricted")
            restricted.type = tarfile.DIRTYPE
            restricted.mode = 0o500
            archive.addfile(restricted)

        runtime = tmp_path / "runtime"
        workspace = runtime / "zephyr_remote_openocd" / "session"
        (workspace / "staged").mkdir(parents=True)
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(runtime)
        result = subprocess.run(
            [sys.executable, str(helper), "stage", str(workspace)],
            env=environment,
            input=archive_stream.getvalue(),
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        assert (workspace / "staged" / "restricted").stat().st_mode & 0o777 == 0o700
        shutil.rmtree(workspace)

    def test_helper_stages_large_archive_through_spooled_stdin(self, tmp_path):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        payload = bytes(range(256)) * 8192
        source = tmp_path / "firmware.bin"
        source.write_bytes(payload)
        archive = build_archive((StagedFile(source, PurePosixPath("firmware.bin")),))

        runtime = tmp_path / "runtime"
        workspace = runtime / "zephyr_remote_openocd" / "session"
        (workspace / "staged").mkdir(parents=True)
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(runtime)
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

    def test_helper_stages_empty_search_root(self, tmp_path):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        local_root = tmp_path / "empty-search"
        local_root.mkdir()
        planner = PathPlanner(())
        planner.plan_directory(local_root, "search_0")
        archive = build_archive(planner.staged_files)

        runtime = tmp_path / "runtime"
        workspace = runtime / "zephyr_remote_openocd" / "session"
        (workspace / "staged").mkdir(parents=True)
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(runtime)
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

    def test_helper_stages_empty_and_nested_directories(self, tmp_path):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        local_root = tmp_path / "search"
        (local_root / "empty").mkdir(parents=True)
        (local_root / "nested" / "also-empty").mkdir(parents=True)
        (local_root / "nested" / "payload.cfg").write_text("payload")
        planner = PathPlanner(())
        planner.plan_directory(local_root, "search_0")
        archive = build_archive(planner.staged_files)

        runtime = tmp_path / "runtime"
        workspace = runtime / "zephyr_remote_openocd" / "session"
        (workspace / "staged").mkdir(parents=True)
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(runtime)
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

    def test_helper_stages_multiple_files_with_interspersed_directories(self, tmp_path):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        tree = tmp_path / "tree"
        nested = tree / "nested"
        nested.mkdir(parents=True)
        first = tree / "first.bin"
        second = nested / "second.bin"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        archive = build_archive(
            (
                StagedDirectory(tree, PurePosixPath("trees/root")),
                StagedFile(first, PurePosixPath("trees/root/first.bin")),
                StagedDirectory(nested, PurePosixPath("trees/root/nested")),
                StagedFile(second, PurePosixPath("trees/root/nested/second.bin")),
            )
        )

        runtime = tmp_path / "runtime"
        workspace = runtime / "zephyr_remote_openocd" / "session"
        (workspace / "staged").mkdir(parents=True)
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(runtime)
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
            "files": list(archive.files),
            "directories": list(archive.directories),
        }
        staged = workspace / "staged"
        assert (staged / "trees/root/first.bin").read_bytes() == b"first"
        assert (staged / "trees/root/nested/second.bin").read_bytes() == b"second"

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
    def test_helper_rejects_unsafe_staging_members(self, tmp_path, members):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        runtime = tmp_path / "runtime"
        workspace = runtime / "zephyr_remote_openocd" / "session"
        (workspace / "staged").mkdir(parents=True)
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(runtime)

        result = subprocess.run(
            [sys.executable, str(helper), "stage", str(workspace)],
            env=environment,
            input=archive_bytes(members),
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert json.loads(result.stdout)["type"] == "ERROR"
        assert tuple((workspace / "staged").iterdir()) == ()

    @pytest.mark.parametrize(
        "response",
        (
            encode_message(
                "STAGED",
                byte_count=999,
                sha256="0" * 64,
                files=["firmware.bin"],
                directories=[],
            ),
            json.dumps(
                {
                    "version": 1,
                    "byte_count": 7,
                    "sha256": "0" * 64,
                    "files": ["firmware.bin"],
                    "directories": [],
                }
            ).encode("utf-8"),
            b"\xff",
        ),
        ids=("mismatched-manifest", "missing-type", "invalid-utf8"),
    )
    def test_backend_rejects_invalid_staging_confirmation(self, response):
        class LocalCommand(_BlockedSshCommand):
            def run_stream(self, host, command, stream, timeout=60):
                stream.read()
                return subprocess.CompletedProcess(command, 0, response, b"")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "firmware.bin"
            source.write_bytes(b"firmware")
            session = RemoteSession(
                RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS),
                DeploymentResult("/helper.py", "digest", False),
            )
            session._helper = _StagingHelper()
            with pytest.raises(SessionError, match="invalid remote staging response"):
                session._stage((StagedFile(source, PurePosixPath("firmware.bin")),))

    def test_backend_rejects_staging_confirmation_with_wrong_digest(self, tmp_path):
        different_payload_digest = hashlib.sha256(b"different firmware").hexdigest()
        response = encode_message(
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
        session = RemoteSession(
            RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )
        session._helper = _StagingHelper()

        with pytest.raises(SessionError):
            session._stage((StagedFile(source, PurePosixPath("firmware.bin")),))

    def test_backend_reports_nonzero_staging_command_and_closes_archive(self):
        diagnostic = "staging destination is unavailable"
        staging_ssh_exit_status = 23
        response = encode_message("ERROR", code="HELPER_ERROR", message=diagnostic)

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
            session = RemoteSession(
                RemoteSessionRequest("local", command, TEST_PROCESS),
                DeploymentResult("/helper.py", "digest", False),
            )
            session._helper = _StagingHelper()

            with pytest.raises(SessionError) as raised:
                session._stage((StagedFile(source, PurePosixPath("firmware.bin")),))

            message = str(raised.value)
            assert str(staging_ssh_exit_status) in message
            assert "HELPER_ERROR" in message
            assert diagnostic in message
            assert command.stream is not None and command.stream.closed

    def test_backend_wraps_invalid_utf8_version_response(self, monkeypatch):
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
            "zephyr_remote_openocd.remote.backend.deploy_helper",
            lambda _command, _host: DeploymentResult("/helper.py", "digest", False),
        )

        with pytest.raises(SessionError, match="invalid remote OpenOCD version response"):
            query_remote_openocd_version(LocalCommand(), "local", ("openocd",))

    def test_helper_applies_requested_environment_before_child_executes(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
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

    @pytest.mark.parametrize(
        ("frame", "close_input"),
        (
            pytest.param(b"not-json\n", False, id="malformed-json"),
            pytest.param(
                b'{"version":2,"type":"STOP"}\n',
                False,
                id="unsupported-version",
            ),
            pytest.param(
                b'{"version":1,"type":"STOP"}',
                True,
                id="missing-lf",
            ),
        ),
    )
    def test_helper_rejects_invalid_command_frame(self, frame, close_input):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
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
                assert error["type"] == "ERROR"
                assert error["code"] == "PROTOCOL_ERROR"
                assert process.wait(timeout=5) == 0
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
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
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
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        required_output_sentinels=(first_sentinel, second_sentinel),
                        readiness_timeout=5,
                    )
                )
                process.stdin.flush()
                events: list[dict[str, Any]] = []
                while not any(event["type"] == "PROCESS_READY" for event in events):
                    events.append(json.loads(read_line(process.stdout)))
                ready = next(event for event in events if event["type"] == "PROCESS_READY")
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
                process.stdin.write(encode_message("STOP"))
                process.stdin.flush()
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
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        result = subprocess.run(
            [sys.executable, str(helper), "openocd-version", sys.executable],
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        message = json.loads(result.stdout)
        assert message["type"] == "OPENOCD_VERSION"
        assert "Python" in message["output"]

    def test_helper_retries_an_openocd_address_collision(self, requires_loopback_listener):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
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
                child = Path(directory) / "collision_child.py"
                child.write_text(
                    "import os, pathlib, socket, sys, time\n"
                    "state = pathlib.Path(os.environ['ZRO_COLLISION_STATE'])\n"
                    "if not state.exists():\n"
                    "    state.write_text(sys.argv[1])\n"
                    "if state.read_text() == sys.argv[1]:\n"
                    "    print(\n"
                    "        'Error: could not bind: Address already in use',\n"
                    "        file=sys.stderr, flush=True\n"
                    "    )\n"
                    "    raise SystemExit(1)\n"
                    "listener = socket.socket()\n"
                    "listener.bind((sys.argv[1], int(sys.argv[2])))\n"
                    "listener.listen()\n"
                    "print(sys.argv[3], flush=True)\n"
                    "time.sleep(30)\n"
                )
                process.stdin.write(
                    start_frame(
                        [
                            sys.executable,
                            str(child),
                            REMOTE_ADDRESS_PLACEHOLDER,
                            str(remote_port),
                            output_sentinel,
                        ],
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        environment={"ZRO_COLLISION_STATE": str(state)},
                        required_output_sentinels=(output_sentinel,),
                        readiness_timeout=5,
                    )
                )
                process.stdin.flush()
                events: list[dict[str, Any]] = []
                while not any(event["type"] == "PROCESS_READY" for event in events):
                    events.append(json.loads(read_line(process.stdout)))
                assert any(
                    event["type"] == "CHILD_OUTPUT"
                    and "address already in use" in event["payload"].casefold()
                    for event in events
                )
                assert any(event["type"] == "PROCESS_READY" for event in events)
                process.stdin.write(encode_message("STOP"))
                process.stdin.flush()
                assert process.wait(timeout=8) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_output_exit_status_and_workspace_cleanup(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
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
                process.stdin.write(start_frame(command))
                process.stdin.flush()
                events = [json.loads(line) for line in read_lines(process.stdout)]
                assert process.wait(timeout=5) == 0
                assert events[0]["type"] == "PROCESS_READY"
                outputs = {
                    (event["stream"], event["payload"])
                    for event in events
                    if event["type"] == "CHILD_OUTPUT" and event["payload"]
                }
                assert outputs == {("stdout", "out"), ("stderr", "err")}
                exit_event = next(event for event in events if event["type"] == "SESSION_CLOSED")
                assert exit_event["returncode"] == OPENOCD_FAILURE_RC
                assert exit_event["reason"] == "process_exit"
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

    def test_helper_signal_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
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
                started = json.loads(read_line(process.stdout))
                assert started["type"] == "PROCESS_READY"
                child_pid = started["child_pid"]
                child_pidfd = os.pidfd_open(child_pid)
                process.terminate()
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

    def test_helper_eof_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
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
                started = json.loads(read_line(process.stdout))
                assert started["type"] == "PROCESS_READY"
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
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
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
                assert events[-1]["type"] == "ERROR"
                assert process.wait(timeout=8) == 0
                assert not workspace.exists()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_backend_returns_child_status_and_relays_output(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand(_BlockedSshCommand):
                argv_prefix = ("local_test",)

                def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                    return managed_popen(
                        [sys.executable, str(helper), "control"],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
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
            remote_process = RemoteProcess(
                (
                    sys.executable,
                    "-c",
                    f'import sys;print("hello");sys.exit({sample_openocd_exit_code})',
                ),
            )
            request = RemoteSessionRequest("local", LocalCommand(), process=remote_process)
            deployment = DeploymentResult(str(helper), "digest", False)
            with patch.object(backend_module, "deploy_helper", return_value=deployment):
                backend = RemoteSession.open(
                    request,
                    output_handler=lambda stream, payload, line_end: output.append(
                        (stream, payload, line_end)
                    ),
                )
            try:
                assert backend.descriptor is not None
                assert ipaddress.ip_address(backend.descriptor.remote_address) in LOOPBACK_RANGE
                assert backend.wait_for_openocd_exit(5) == sample_openocd_exit_code
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
                backend.close()

    def test_backend_surfaces_helper_descendant_warning(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            descendant_ready = Path(directory) / "descendant-ready"

            class LocalCommand(_BlockedSshCommand):
                argv_prefix = ("local_test",)

                def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                    return managed_popen(
                        [sys.executable, str(helper), "control"],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )

            child_code = (
                "import os, signal, sys, time\n"
                "if os.fork() == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "    open(sys.argv[1], 'w', encoding='ascii').close()\n"
                "    time.sleep(30)\n"
                "else:\n"
                "    while not os.path.exists(sys.argv[1]):\n"
                "        time.sleep(0.01)\n"
                "    print(sys.argv[2], flush=True)\n"
                "    time.sleep(30)\n"
            )
            remote_process = RemoteProcess(
                (sys.executable, "-c", child_code, str(descendant_ready), "ZRO_DESCENDANT_READY"),
                required_output_sentinels=("ZRO_DESCENDANT_READY",),
            )
            output = []
            backend = _opened_session(
                RemoteSessionRequest("local", LocalCommand(), process=remote_process),
                DeploymentResult(str(helper), "digest", False),
                lambda stream, payload, line_end: output.append((stream, payload, line_end)),
            )
            try:
                backend._start_process(())
                backend.close()
            finally:
                with suppress(BaseException):
                    backend.close()

            warning = "warning: terminating remaining OpenOCD process-group members:"
            assert any(
                stream == "stderr" and warning in payload for stream, payload, _line_end in output
            )

    @pytest.mark.parametrize(
        ("event_stream_tail", "exit_code", "expected", "expected_openocd_result"),
        (
            (
                json.dumps(
                    {
                        "version": 1,
                        "type": "SESSION_CLOSED",
                        "reason": "requested",
                        "returncode": None,
                    },
                    separators=(",", ":"),
                ),
                0,
                None,
                None,
            ),
            (
                json.dumps(
                    {
                        "version": 1,
                        "type": "SESSION_CLOSED",
                        "reason": "process_exit",
                        "returncode": 7,
                    },
                    separators=(",", ":"),
                ),
                0,
                None,
                OPENOCD_FAILURE_RC,
            ),
            (
                '{"version":1,"type":"SESSION_CLOSED","reason":"requested",'
                '"returncode":null}\n'
                '{"version":1,"type":"ERROR","code":"CLEANUP",'
                '"message":"cleanup failed"}',
                0,
                ("unexpected ERROR event in closed state",),
                None,
            ),
            (
                '{"version":1,"type":"SESSION_CLOSED","reason":"requested",'
                '"returncode":null}\nnot-json',
                0,
                ("malformed protocol message",),
                None,
            ),
            (
                json.dumps(
                    {"version": 1, "type": "ERROR", "code": "CLEANUP", "message": "cleanup failed"},
                    separators=(",", ":"),
                ),
                7,
                ("remote helper error",),
                None,
            ),
            (
                json.dumps(
                    {
                        "version": 1,
                        "type": "SESSION_CLOSED",
                        "reason": "requested",
                        "returncode": None,
                    },
                    separators=(",", ":"),
                ),
                7,
                ("status 7", "requested shutdown"),
                None,
            ),
            (
                json.dumps(
                    {
                        "version": 1,
                        "type": "SESSION_CLOSED",
                        "reason": "requested",
                        "returncode": 0,
                    },
                    separators=(",", ":"),
                ),
                0,
                ("invalid required fields for SESSION_CLOSED",),
                None,
            ),
            (
                json.dumps(
                    {
                        "version": 1,
                        "type": "SESSION_CLOSED",
                        "reason": "process_exit",
                        "returncode": 7,
                    },
                    separators=(",", ":"),
                ),
                7,
                ("status 7", "process_exit shutdown"),
                OPENOCD_FAILURE_RC,
            ),
            (None, 7, ("did not produce SESSION_CLOSED",), None),
        ),
        ids=(
            "requested-success",
            "process-exit-after-stop-success",
            "requested-then-error",
            "requested-then-malformed",
            "error-and-nonzero",
            "requested-session-close-nonzero",
            "malformed-session-close",
            "process-exit-with-nonzero-helper",
            "nonzero-without-session-close",
        ),
    )
    def test_helper_client_close_validates_helper_shutdown(
        self,
        event_stream_tail,
        exit_code,
        expected,
        expected_openocd_result,
    ):
        event_stream_tail = "" if event_stream_tail is None else event_stream_tail + "\n"
        helper_code = f"""
import json
import sys

created = {{
    "version": 1,
    "type": "SESSION_CREATED",
    "helper": "test",
    "session_id": "id",
    "remote_workspace": "/workspace",
}}
print(json.dumps(created, separators=(",", ":")), flush=True)
sys.stdin.buffer.readline()
sys.stdout.write({event_stream_tail!r})
sys.stdout.flush()
sys.exit({exit_code})
"""

        class LocalCommand(_BlockedSshCommand):
            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                return managed_popen(
                    [sys.executable, "-c", helper_code],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

        helper_client = _opened_helper_client(
            RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )
        try:
            if expected is None:
                result = helper_client.close()
                assert result.error is None
                assert helper_client.openocd_returncode == expected_openocd_result
            else:
                result = helper_client.close()
                assert result.error is not None
                assert all(fragment in str(result.error) for fragment in expected)
        finally:
            with suppress(BaseException):
                helper_client.close()

    def test_helper_client_rejects_requested_session_close_before_local_stop(self):
        helper_code = """
import json
import sys

print(
    json.dumps(
        {
            "version": 1,
            "type": "SESSION_CREATED",
            "helper": "test",
            "session_id": "id",
            "remote_workspace": "/workspace",
        }
    ),
    flush=True,
)
print(
    json.dumps(
        {"version": 1, "type": "SESSION_CLOSED", "reason": "requested", "returncode": None}
    ),
    flush=True,
)
sys.stdin.buffer.read()
"""

        class LocalCommand(_BlockedSshCommand):
            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                return managed_popen(
                    [sys.executable, "-c", helper_code],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

        helper_client = _opened_helper_client(
            RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )
        session_close_consumed = threading.Event()
        dispatch = helper_client._dispatch

        def observe_session_close(event):
            dispatch(event)
            if event["type"] == "SESSION_CLOSED":
                session_close_consumed.set()

        with patch.object(helper_client, "_dispatch", side_effect=observe_session_close):
            helper_client._start_event_drain()
            assert helper_client._reader_thread is not None
            assert session_close_consumed.wait(5)
        try:
            result = helper_client.close()
            assert isinstance(result.error, SessionError)
        finally:
            with suppress(BaseException):
                helper_client.close()

    def test_helper_client_reports_helper_failure_after_close_event(self):
        helper_code = f"""
import json
import sys

events = (
    {{
        "version": 1,
        "type": "SESSION_CREATED",
        "helper": "test",
        "session_id": "id",
        "remote_workspace": "/workspace",
    }},
    {{"version": 1, "type": "PROCESS_READY", "remote_address": "127.64.0.1", "child_pid": 1}},
    {{"version": 1, "type": "SESSION_CLOSED", "reason": "process_exit", "returncode": 0}},
)
for event in events:
    print(json.dumps(event, separators=(",", ":")), flush=True)
sys.exit({HELPER_FAILURE_RC})
"""

        class LocalCommand(_BlockedSshCommand):
            process: Any = None

            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                process = managed_popen(
                    [sys.executable, "-c", helper_code],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                inner.process = process
                return process

        command = LocalCommand()
        helper_client = _opened_helper_client(
            RemoteSessionRequest("local", command, TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )
        try:
            helper_client.start_process(TEST_PROCESS, ())
            command.process.wait(timeout=5)
            with pytest.raises(SessionError):
                helper_client.recorded_openocd_exit()
            assert helper_client.openocd_returncode == 0
        finally:
            with suppress(BaseException):
                helper_client.close()

    def test_helper_client_close_waits_for_child_kill_fallback(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            child_pid_path = Path(directory) / "child.pid"

            class LocalCommand(_BlockedSshCommand):
                argv_prefix = ("local_test",)
                process: Any = None

                def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                    process = managed_popen(
                        [sys.executable, str(helper), "control"],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
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
            remote_process = RemoteProcess(
                (sys.executable, "-c", child_code, output_sentinel, str(child_pid_path)),
                required_output_sentinels=(output_sentinel,),
            )
            command = LocalCommand()
            helper_client = _opened_helper_client(
                RemoteSessionRequest("local", command, process=remote_process),
                DeploymentResult(str(helper), "digest", False),
            )
            workspace = Path(helper_client.allocation.remote_workspace)
            child_pid = None
            child_pidfd = None
            try:
                helper_client.start_process(remote_process, ())
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                child_pidfd = os.pidfd_open(child_pid)

                result = helper_client.close()

                assert result.error is None
                assert command.process.returncode == 0
                assert not workspace.exists()
                _assert_pidfd_exited(child_pidfd)
            finally:
                if child_pidfd is not None:
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(child_pidfd, signal.SIGKILL)
                    os.close(child_pidfd)

    def test_backend_reader_failure_requires_lifecycle_cleanup(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand(_BlockedSshCommand):
                def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                    return managed_popen(
                        [sys.executable, str(helper), "control"],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
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

            remote_process = RemoteProcess(
                (
                    sys.executable,
                    "-c",
                    'import sys,time;print("hello",flush=True);time.sleep(30)',
                ),
            )
            request = RemoteSessionRequest("local", LocalCommand(), process=remote_process)

            output_error = RuntimeError()

            def fail_on_output(_stream, _payload, _line_end):
                raise output_error

            backend = _opened_session(
                request,
                DeploymentResult(str(helper), "digest", False),
                fail_on_output,
            )
            try:
                backend._stage(())
                backend._start_process(())
                with pytest.raises(SessionError) as raised:
                    backend.wait_for_openocd_exit(5)
                assert raised.value.__cause__ is output_error
                assert not backend.closed
                with pytest.raises(SessionError) as raised:
                    backend.close()
                assert raised.value.__cause__ is output_error
            finally:
                with suppress(BaseException):
                    backend.close()


def _opened_helper_client(request, deployment, output_handler=None):
    return _HelperClient.open(
        request.ssh_command,
        request.host,
        deployment,
        output_handler=output_handler,
    )


def _opened_session(request, deployment, output_handler=None):
    session = RemoteSession(request, deployment, output_handler)
    session._helper = _opened_helper_client(request, deployment, output_handler)
    return session
