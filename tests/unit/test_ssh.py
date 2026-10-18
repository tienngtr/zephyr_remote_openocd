# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import FrameType
from typing import Any, BinaryIO, cast, override
from unittest.mock import Mock, create_autospec, patch

import pytest
from zephyr_remote_openocd.controller import _ssh as ssh_module
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._forwarding import ForwardManager
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    SshCommand,
    SshForwardSpec,
    SshProcessStartError,
    stop_process,
)
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionController,
    SessionError,
    SessionPlan,
)
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.protocol.outcome import Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import encode_frame

from tests.control_support import HelperPeerProcess
from tests.process_support import cleanup_on_exit
from tests.protocol_support import terminal_message

STREAM_CHILD_EXIT_CODE = 4
SAMPLE_OPENOCD_EXIT_CODE = 6


class _PopenOnlySshCommand(SshCommand):
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
    def run_stream(
        self,
        host: str,
        remote_command: str,
        stream: BinaryIO,
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("run_stream() is not expected in this test")


def _pipe_stream(payload: bytes) -> BinaryIO:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb", buffering=0) as stream:
        stream.write(payload)
    return os.fdopen(read_fd, "rb", buffering=0)


def test_fixed_arguments_are_preserved_without_a_shell() -> None:
    ssh = SshCommand(("custom-ssh", "-F", "/a file", "-o", "BatchMode=yes"))
    assert ssh.argv("board-lab", "printf marker") == [
        "custom-ssh",
        "-F",
        "/a file",
        "-o",
        "BatchMode=yes",
        "board-lab",
        "printf marker",
    ]


@patch("subprocess.Popen.__init__", autospec=True, side_effect=OSError("capture argv"))
def test_long_lived_process_preserves_explicit_path_and_fixed_arguments(popen: Mock) -> None:
    with pytest.raises(OSError):
        SshCommand(("/opt/client/custom-ssh", "-F", "/a file")).popen("host", "serve")
    popen.assert_called_once_with(
        popen.call_args.args[0],
        ["/opt/client/custom-ssh", "-F", "/a file", "host", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_bare_alternate_executable_is_resolved_through_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "custom-ssh"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    result = SshCommand(("custom-ssh", "--fixed")).run("target", "remote command")

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--fixed", "target", "remote command"]


@pytest.mark.parametrize(
    "start_failure, search_path",
    ((False, False), (True, False), (True, True)),
    ids=("success", "missing-executable", "missing-executable-search-path"),
)
def test_long_lived_launch_restores_calling_thread_signal_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start_failure: bool, search_path: bool
) -> None:
    if search_path:
        # A bare executable uses the real fork/exec search path even on newer
        # runtimes whose absolute-path launches can use posix_spawn.
        monkeypatch.setenv("PATH", str(tmp_path))

    def launch() -> None:
        original = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
        expected = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        try:
            if start_failure:
                executable = "missing-ssh" if search_path else str(tmp_path / "missing-ssh")
                with pytest.raises(FileNotFoundError):
                    SshCommand((executable,)).popen("host", "serve")
            else:
                process = SshCommand((sys.executable, "-c", "pass")).popen("host", "serve")
                stop_process(process)
            assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == expected
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, original)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(launch).result(timeout=30)


@pytest.fixture
def acquired_transports() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Capture real subprocess acquisition and provide assertion-failure cleanup."""
    previous_profile = sys.getprofile()
    processes: list[subprocess.Popen[bytes]] = []

    def observe_acquisition(frame: FrameType, event: str, _arg: object) -> None:
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            processes.append(cast(subprocess.Popen[bytes], frame.f_locals["self"]))

    sys.setprofile(observe_acquisition)
    try:
        yield processes
    finally:
        sys.setprofile(previous_profile)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _assert_transport_disposed(processes: list[subprocess.Popen[bytes]]) -> None:
    assert len(processes) == 1
    process = processes[0]
    assert process.returncode is not None
    assert all(
        stream is not None and stream.closed
        for stream in (process.stdin, process.stdout, process.stderr)
    )


@pytest.mark.parametrize("failure_type", (MemoryError, KeyboardInterrupt, OSError))
def test_stderr_owner_construction_failure_disposes_raw_transport(
    monkeypatch: pytest.MonkeyPatch,
    acquired_transports: list[subprocess.Popen[bytes]],
    failure_type: type[BaseException],
) -> None:
    failure = failure_type("stderr owner construction failed")
    monkeypatch.setattr(
        ssh_module.threading, "Thread", create_autospec(threading.Thread, side_effect=failure)
    )
    command = SshCommand((sys.executable, "-c", "import sys; sys.stdin.buffer.read()"))
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

    with pytest.raises((failure_type, SshProcessStartError)) as raised:
        command.popen("host", "serve")

    if isinstance(raised.value, SshProcessStartError):
        assert raised.value.cause is failure and not raised.value.cleanup_errors
    else:
        assert raised.value is failure
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == previous_mask
    _assert_transport_disposed(acquired_transports)


def test_failed_stderr_construction_retains_primary_when_mask_restoration_fails(
    monkeypatch: pytest.MonkeyPatch, acquired_transports: list[subprocess.Popen[bytes]]
) -> None:
    failure = MemoryError("stderr owner construction failed")
    restoration_failure = OSError("launch signal-mask restoration failed")
    pthread_sigmask = signal.pthread_sigmask

    def fail_constructor(*_args: object, **_kwargs: object) -> None:
        raise failure

    def fail_restoration(how: int, mask: Iterable[int]) -> set[int | signal.Signals]:
        previous = pthread_sigmask(how, mask)
        if how == signal.SIG_SETMASK:
            raise restoration_failure
        return previous

    monkeypatch.setattr(ssh_module.threading, "Thread", fail_constructor)
    expected_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    monkeypatch.setattr(signal, "pthread_sigmask", fail_restoration)
    with pytest.raises(MemoryError) as raised:
        SshCommand((sys.executable, "-c", "import sys; sys.stdin.buffer.read()")).popen(
            "host", "serve"
        )
    monkeypatch.undo()
    assert raised.value is failure
    assert any(str(restoration_failure) in note for note in raised.value.__notes__)
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == expected_mask
    _assert_transport_disposed(acquired_transports)


def test_run_stream_passes_file_as_stdin_and_captures_output(tmp_path: Path) -> None:
    code = (
        "import sys;"
        "sys.stdout.buffer.write(sys.stdin.buffer.read());"
        "sys.stderr.buffer.write(b'x' * 100000);"
        "sys.stderr.flush();"
        f"raise SystemExit({STREAM_CHILD_EXIT_CODE})"
    )
    source_path = tmp_path / "payload.bin"
    source_path.write_bytes(b"payload")
    with source_path.open("rb") as source:
        result = SshCommand((sys.executable, "-c", code)).run_stream("host", "ignored", source)
        assert not source.closed
    assert result.returncode == STREAM_CHILD_EXIT_CODE
    assert result.stdout == b"payload"
    assert result.stderr == b"x" * 100000


@pytest.mark.timeout(10)
def test_stderr_tail_is_best_effort_when_drain_has_not_reached_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_chunk_read = threading.Event()
    release_eof = threading.Event()

    class Stream(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        @override
        def read(self, _size: int | None = -1, /) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"prefix"
            # The previous read has been appended before the drain reads again.
            first_chunk_read.set()
            assert release_eof.wait(5)
            return b""

        @override
        def close(self) -> None:
            release_eof.set()

    drain = ssh_module.StderrDrain(Stream())
    original_wait = drain._finished.wait
    monkeypatch.setattr(drain._finished, "wait", lambda _timeout=None: False)
    drain.start()
    try:
        assert first_chunk_read.wait(5)
        assert drain.tail() == b"prefix"
        assert not drain._finished.is_set()
        release_eof.set()
        assert original_wait(5)
    finally:
        release_eof.set()
        drain._thread.join(timeout=5)
        assert not drain._thread.is_alive()


@pytest.mark.timeout(10)
def test_stderr_tail_waits_for_delayed_eof_with_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_chunk_read = threading.Event()
    release_suffix = threading.Event()
    suffix_read = threading.Event()

    class Stream(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        @override
        def read(self, _size: int | None = -1, /) -> bytes:
            self.reads += 1
            if self.reads == 1:
                first_chunk_read.set()
                return b"prefix"
            if self.reads == 2:
                assert release_suffix.wait(5)
                suffix_read.set()
                return b"suffix"
            return b""

        @override
        def close(self) -> None:
            release_suffix.set()

    drain = ssh_module.StderrDrain(Stream())
    wait_timeouts: list[float | None] = []
    original_wait = drain._finished.wait

    def wait(timeout: float | None = None) -> bool:
        wait_timeouts.append(timeout)
        release_suffix.set()
        return original_wait(timeout)

    monkeypatch.setattr(drain._finished, "wait", wait)
    drain.start()
    try:
        assert first_chunk_read.wait(5)
        assert drain.tail() == b"prefixsuffix"
        assert suffix_read.is_set()
        assert wait_timeouts
        assert all(
            timeout is not None
            and math.isfinite(timeout)
            and 0 < timeout <= ssh_module._SSH_STDERR_JOIN_TIMEOUT
            for timeout in wait_timeouts
        )
    finally:
        release_suffix.set()
        drain._thread.join(timeout=5)
        assert not drain._thread.is_alive()


def test_process_cleanup_kills_after_graceful_termination_times_out() -> None:
    class Process:
        def __init__(self) -> None:
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.returncode: int | None = None
            self.terminated = False
            self.killed = False
            self.graceful_wait_timed_out = False
            self.kill_wait_completed = False
            self.stderr_closed = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and math.isfinite(timeout) and timeout > 0
            if not self.killed:
                self.graceful_wait_timed_out = True
                raise subprocess.TimeoutExpired("fake-ssh", timeout)
            self.kill_wait_completed = True
            returncode = -signal.SIGKILL
            self.returncode = returncode
            return returncode

        def kill(self) -> None:
            self.killed = True

        def close_stderr(self) -> None:
            self.stderr_closed = True

    process = Process()

    stop_process(process)

    assert process.terminated
    assert process.graceful_wait_timed_out
    assert process.killed
    assert process.kill_wait_completed
    assert process.returncode is not None
    assert process.stderr_closed
    assert process.stdin.closed
    assert process.stdout.closed


def test_process_cleanup_retains_graceful_timeout_after_kill_failure() -> None:
    kill_error = RuntimeError("kill failed")
    timeout_error = subprocess.TimeoutExpired("fake-ssh", 5)

    class Process:
        stdin = None
        stdout = None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and math.isfinite(timeout) and timeout > 0
            raise timeout_error

        def kill(self) -> None:
            raise kill_error

        def close_stderr(self) -> None:
            pass

    with pytest.raises(RuntimeError) as raised:
        stop_process(Process())

    assert raised.value is kill_error
    assert any(str(timeout_error) in note for note in getattr(raised.value, "__notes__", ()))


class _ForwardCommand(_PopenOnlySshCommand):
    processes: Iterator[Any]
    calls: list[tuple[str, str, SshForwardSpec | None]]

    def __init__(self, *processes: HelperPeerProcess) -> None:
        super().__init__()
        object.__setattr__(self, "processes", iter(processes))
        object.__setattr__(self, "calls", [])

    @override
    def popen(
        self,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired: Callable[[ManagedSshProcess], None] | None = None,
    ) -> Any:
        self.calls.append((host, remote_command, local_forward))
        acquired_process = next(self.processes)
        if on_acquired is not None:
            on_acquired(acquired_process)
        return acquired_process


@pytest.mark.timeout(10)
def test_initial_forward_failure_consumes_session_close_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_seen = threading.Event()
    forward_error = SessionError("initial forwarding failed")
    helper = HelperPeerProcess(
        (
            encode_frame("ATTEMPT", argv=["test-process"]),
            encode_frame("READY", remote_address="127.64.0.1", child_pid=1),
        ),
        later=(terminal_message(Trigger.CHILD_EXIT, returncode=SAMPLE_OPENOCD_EXIT_CODE),),
    )
    command = _ForwardCommand(helper)
    request = SessionPlan(
        "host",
        command,
        ProcessSpec(("child",)),
        forwards=(ForwardSpec("gdb", 3333, 3333),),
    )
    deployment = DeploymentResult("/helper.py", "digest", False)
    dispatch = HelperConnection._dispatch

    def deploy(_ssh_command: SshCommand, _host: str) -> DeploymentResult:
        return deployment

    monkeypatch.setattr(session_module, "deploy_helper", deploy)

    def stage(
        _command: SshCommand,
        _host: str,
        remote_command: str,
        stream: BinaryIO,
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        assert stream.read()
        return subprocess.CompletedProcess(
            remote_command,
            0,
            encode_frame(
                "STAGED",
                byte_count=0,
                sha256=hashlib.sha256(b"").hexdigest(),
                files=[],
                directories=[],
            ),
            b"",
        )

    monkeypatch.setattr(_ForwardCommand, "run_stream", stage)

    def observe_close(helper_client: HelperConnection, event: dict[str, object]) -> None:
        dispatch(helper_client, event)
        if event["type"] == "SESSION_ENDED":
            close_seen.set()

    def fail_forwards(
        _manager: ForwardManager, _forwards: Iterable[ForwardSpec], _address: str
    ) -> None:
        # The helper reports child exit while required forwarding is starting.
        helper.release_later()
        assert close_seen.wait(5)
        raise forward_error

    monkeypatch.setattr(HelperConnection, "_dispatch", observe_close)
    monkeypatch.setattr(ForwardManager, "start", fail_forwards)

    with pytest.raises(SessionError) as raised:
        session = SessionController.prepare(request)
        with cleanup_on_exit(session.close):
            session.acquire()

    assert str(SAMPLE_OPENOCD_EXIT_CODE) in str(raised.value)
    assert raised.value is not forward_error
    assert close_seen.is_set()


def test_acquisition_rollback_uses_bounded_process_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    startup_error = OSError("stderr drain startup failed")
    waits: list[float | None] = []
    wait_errors: list[subprocess.TimeoutExpired] = []

    class Process:
        pid = 1
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            waits.append(timeout)
            assert timeout is not None
            error = subprocess.TimeoutExpired("configured-ssh", timeout)
            wait_errors.append(error)
            raise error

    process = Process()

    def fail_start(_drain: ssh_module.StderrDrain) -> None:
        raise startup_error

    monkeypatch.setattr(ssh_module, "_OwnedSshPopen", lambda *_args: process)
    monkeypatch.setattr(ssh_module.StderrDrain, "start", fail_start)
    with pytest.raises(ssh_module.SshProcessStartError) as raised:
        SshCommand(("configured-ssh",)).popen("host", "ignored")

    assert waits and all(timeout is not None and 0 < timeout < math.inf for timeout in waits)
    assert raised.value.cause is startup_error
    assert set(raised.value.cleanup_errors) == set(wait_errors)
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed


@pytest.mark.parametrize(
    "failure_phase, failure_type",
    (("drain-construction", OSError), ("drain-start", OSError), ("drain-start", RuntimeError)),
)
def test_acquisition_rollback_attempts_every_stream_and_retains_all_failures(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str, failure_type: type[BaseException]
) -> None:
    startup_error = failure_type("stderr drain startup failed")
    terminate_error = OSError("process terminate failed")
    kill_error = OSError("process kill failed")
    stdin_error = OSError("stdin close failed")
    stdout_error = OSError("stdout close failed")
    stderr_error = OSError("stderr close failed")
    cleanup_errors = (terminate_error, kill_error, stdin_error, stdout_error, stderr_error)
    for error in cleanup_errors:
        error.add_note(f"retained detail for {error}")

    class FailingStream(io.BytesIO):
        def __init__(self, error: BaseException) -> None:
            super().__init__()
            self.error = error
            self.close_attempts = 0

        @override
        def close(self) -> None:
            self.close_attempts += 1
            super().close()
            raise self.error

    class Process:
        pid = 1
        returncode: int | None = None
        stdin = FailingStream(stdin_error)
        stdout = FailingStream(stdout_error)
        stderr = FailingStream(stderr_error)

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            raise terminate_error

        def kill(self) -> None:
            raise kill_error

        def wait(self, timeout: float | None = None) -> int:
            raise AssertionError("wait is not expected after failed process signals")

    process = Process()

    def fail_start(_drain: ssh_module.StderrDrain) -> None:
        raise startup_error

    monkeypatch.setattr(ssh_module, "_OwnedSshPopen", lambda *_args: process)
    if failure_phase == "drain-construction":
        monkeypatch.setattr(
            ssh_module.threading,
            "Thread",
            create_autospec(threading.Thread, side_effect=startup_error),
        )
    else:
        monkeypatch.setattr(ssh_module.StderrDrain, "start", fail_start)
    with pytest.raises((ssh_module.SshProcessStartError, failure_type)) as raised:
        SshCommand(("configured-ssh",)).popen("host", "ignored")

    try:
        assert [
            stream.close_attempts for stream in (process.stdin, process.stdout, process.stderr)
        ] == [1, 1, 1]
        if isinstance(startup_error, OSError):
            assert isinstance(raised.value, ssh_module.SshProcessStartError)
            assert raised.value.cause is startup_error
            assert set(raised.value.cleanup_errors) == set(cleanup_errors)
        else:
            assert raised.value is startup_error
        retained = "\n".join(startup_error.__notes__)
        assert all(error.__notes__[0] in retained for error in cleanup_errors)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                io.BytesIO.close(stream)
