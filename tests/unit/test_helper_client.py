# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import os
import signal
import subprocess
import threading
from typing import Any, BinaryIO, cast, override

import pytest
from zephyr_remote_openocd.remote import helper_client as helper_client_module
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.helper_client import _HelperClient
from zephyr_remote_openocd.remote.model import RemoteProcess, RemoteSessionRequest
from zephyr_remote_openocd.remote.protocol import ProtocolError, decode_message, encode_message
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, SshCommand

OPENOCD_FAILURE_RC = 7


class _PopenOnlySshCommand(SshCommand):
    @override
    def run(
        self,
        host: str,
        remote_command: str,
        *,
        input_data: bytes | None = None,
        timeout: float = 15,
    ):
        raise AssertionError("run() is not expected in this test")

    @override
    def run_stream(
        self,
        host: str,
        remote_command: str,
        stream: BinaryIO,
        *,
        timeout: float = 60,
    ):
        raise AssertionError("run_stream() is not expected in this test")


def _helper_client() -> _HelperClient:
    return _HelperClient(SshCommand(), "host", DeploymentResult("/helper.py", "digest", False))


class _RecordingInput:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False

    def write(self, payload: bytes) -> int:
        self.written.extend(payload)
        return len(payload)

    @staticmethod
    def flush() -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _EventProcess:
    def __init__(self, events: tuple[bytes, ...]) -> None:
        self.args = ("fake-helper",)
        self.stdin = _RecordingInput()
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"".join(events))
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stderr = io.BytesIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -signal.SIGKILL

    @staticmethod
    def stderr_tail() -> bytes:
        return b""

    def close_stderr(self) -> None:
        self.stderr.close()


def _open_helper_client(process: _EventProcess) -> _HelperClient:
    class Command(_PopenOnlySshCommand):
        @override
        def popen(self, host: str, remote_command: str, *extra_args: str) -> Any:
            del host, remote_command, extra_args
            return process

    return _HelperClient.open(Command(), "host", DeploymentResult("/helper.py", "digest", False))


def _open_helper_client_with_events(
    *events: bytes,
) -> tuple[_HelperClient, _EventProcess]:
    process = _EventProcess(
        (
            encode_message(
                "SESSION_CREATED",
                helper="fake",
                session_id="session",
                remote_workspace="/workspace",
            ),
            *events,
        )
    )
    return _open_helper_client(process), process


def test_open_rejects_initial_frame_without_lf():
    payload = encode_message(
        "SESSION_CREATED",
        helper="fake",
        session_id="session",
        remote_workspace="/workspace",
    ).rstrip(b"\n")

    with pytest.raises(ProtocolError):
        _open_helper_client(_EventProcess((payload,)))


@pytest.mark.timeout(10)
def test_recorded_openocd_exit_is_available_while_reader_remains_alive():
    helper_client = _helper_client()
    close_recorded = threading.Event()
    release_reader = threading.Event()

    def consume_close() -> None:
        helper_client._dispatch(
            {
                "type": "SESSION_CLOSED",
                "reason": "process_exit",
                "returncode": OPENOCD_FAILURE_RC,
            }
        )
        close_recorded.set()
        release_reader.wait()

    reader = threading.Thread(target=consume_close)
    reader.start()
    assert close_recorded.wait(5)
    try:
        assert reader.is_alive()
        assert helper_client.recorded_openocd_exit() == OPENOCD_FAILURE_RC
    finally:
        release_reader.set()
        reader.join(timeout=5)

    assert not reader.is_alive()


def test_close_waits_when_process_exit_wins_stop_race(monkeypatch):
    wait_called = threading.Event()

    class Process:
        args = ("fake-helper",)

        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            wait_called.set()
            self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -signal.SIGKILL

        @staticmethod
        def stderr_tail():
            return b""

        def close_stderr(self):
            self.stderr.close()

    class Reader(threading.Thread):
        @override
        def join(self, timeout=None):
            del timeout

        @override
        def is_alive(self):
            return False

    helper_client = _helper_client()
    process = Process()
    helper_client._process = cast(ManagedSshProcess, process)
    helper_client._reader_thread = Reader()

    request_stop = helper_client._observations.request_stop

    def observe_process_exit(write_stop):
        helper_client._observations.record_close("process_exit", 0)
        return request_stop(write_stop)

    monkeypatch.setattr(helper_client._observations, "request_stop", observe_process_exit)

    result = helper_client.close()

    assert result.error is None
    assert wait_called.is_set()
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_startup_error_ends_session_without_stop_or_missing_close_failure():
    helper_client, process = _open_helper_client_with_events(
        encode_message("ERROR", code="FAILED", message="startup failed"),
    )

    with pytest.raises(SessionError, match="remote helper error: startup failed"):
        helper_client.start_process(RemoteProcess(("child",)), ())

    result = helper_client.close()

    assert result.error is None
    commands = [decode_message(bytes(line))["type"] for line in process.stdin.written.splitlines()]
    assert commands == ["START"]


@pytest.mark.timeout(10)
def test_unexpected_requested_close_is_reported_by_foreground_result():
    helper_client, _process = _open_helper_client_with_events(
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        encode_message("SESSION_CLOSED", reason="requested", returncode=None),
    )
    helper_client.start_process(RemoteProcess(("child",)), ())
    helper_client.wait_for_change(5)

    try:
        with pytest.raises(SessionError, match="helper reported SESSION_CLOSED") as raised:
            helper_client.recorded_openocd_exit()

        assert raised.value.__cause__ is None
    finally:
        helper_client.close()


@pytest.mark.timeout(10)
def test_background_error_ends_session_without_sending_stop():
    helper_client, process = _open_helper_client_with_events(
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        encode_message("ERROR", code="FAILED", message="background failed"),
    )
    helper_client.start_process(RemoteProcess(("child",)), ())

    helper_client.wait_for_change(5)
    result = helper_client.close()

    assert isinstance(result.error, SessionError)
    assert result.error.__cause__ is None
    commands = [decode_message(bytes(line))["type"] for line in process.stdin.written.splitlines()]
    assert commands == ["START"]


@pytest.mark.timeout(10)
def test_observed_background_error_is_not_reported_again_on_close():
    helper_client, process = _open_helper_client_with_events(
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        encode_message("ERROR", code="FAILED", message="background failed"),
    )
    helper_client.start_process(RemoteProcess(("child",)), ())
    helper_client.wait_for_change(5)

    for _attempt in range(2):
        with pytest.raises(SessionError):
            helper_client.recorded_openocd_exit()

    assert helper_client.close().error is None
    commands = [decode_message(bytes(line))["type"] for line in process.stdin.written.splitlines()]
    assert commands == ["START"]


def test_close_keeps_helper_error_primary_when_forced_disposal_also_fails(monkeypatch):
    helper_client, _process = _open_helper_client_with_events(
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        encode_message("ERROR", code="FAILED", message="background failed"),
    )
    helper_client.start_process(RemoteProcess(("child",)), ())
    helper_client.wait_for_change(5)

    cleanup_error = RuntimeError("forced disposal failed")

    def fail_stop(_process, *, close_streams=True):
        del close_streams
        raise cleanup_error

    monkeypatch.setattr(helper_client_module, "_stop_process", fail_stop)

    result = helper_client.close()

    assert isinstance(result.error, SessionError)
    assert str(result.error) == "remote helper error: background failed"
    assert result.error.__cause__ is None
    assert result.cleanup_errors == (cleanup_error,)
    assert any("helper cleanup also failed" in note for note in result.error.__notes__)


def test_reader_failure_takes_precedence_over_known_openocd_result():
    helper_client = _helper_client()
    helper_client._observations.record_close("process_exit", OPENOCD_FAILURE_RC)
    reader_error = RuntimeError("protocol failed")
    helper_client._observations.record_reader_failure(reader_error)

    with pytest.raises(SessionError) as raised:
        helper_client.recorded_openocd_exit()

    assert raised.value.__cause__ is reader_error


def test_close_keeps_stop_failure_primary_when_forced_disposal_also_fails():
    graceful_stop_error = RuntimeError("graceful stop failed")
    forced_stop_error = RuntimeError("forced stop failed")

    class FailingStdin:
        closed = False

        def write(self, _payload):
            raise graceful_stop_error

        @staticmethod
        def flush():
            pass

        def close(self):
            self.closed = True

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.stdin = FailingStdin()
            self.stdout = io.BytesIO(
                encode_message("SESSION_CLOSED", reason="requested", returncode=None)
            )
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            raise forced_stop_error

        def kill(self):
            self.returncode = -signal.SIGKILL

        @staticmethod
        def stderr_tail():
            return b""

        @staticmethod
        def close_stderr():
            pass

    helper_client = _helper_client()
    helper_client._process = cast(ManagedSshProcess, Process())

    result = helper_client.close()

    assert result.error is graceful_stop_error
    assert result.cleanup_errors == (forced_stop_error,)
    assert any("helper cleanup also failed" in note for note in graceful_stop_error.__notes__)


def test_close_disposes_helper_when_initial_status_observation_fails():
    observation_error = RuntimeError("helper status failed")

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None
            self.initial_status_failure_pending = True

        def poll(self):
            if self.initial_status_failure_pending:
                self.initial_status_failure_pending = False
                raise observation_error
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -signal.SIGKILL

        def wait(self, timeout=None):
            del timeout
            return self.returncode

        def close_stderr(self):
            self.stderr.close()

    process = Process()
    helper_client = _helper_client()
    helper_client._process = cast(ManagedSshProcess, process)

    result = helper_client.close()

    assert result.error is observation_error
    assert result.cleanup_errors == ()
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_close_disposes_helper_when_reader_join_fails():
    join_error = RuntimeError("helper reader join failed")

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = 0

        def poll(self):
            return self.returncode

        def close_stderr(self):
            self.stderr.close()

    class Reader(threading.Thread):
        def __init__(self):
            super().__init__()
            self.join_failure_pending = True

        @override
        def join(self, timeout=None):
            del timeout
            if self.join_failure_pending:
                self.join_failure_pending = False
                raise join_error

        @override
        def is_alive(self):
            return False

    process = Process()
    helper_client = _helper_client()
    helper_client._process = cast(ManagedSshProcess, process)
    helper_client._reader_thread = Reader()
    helper_client._observations.record_close("process_exit", 0)

    result = helper_client.close()

    assert result.error is None
    assert result.cleanup_errors == (join_error,)
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_close_preserves_cleanup_error_when_final_status_observation_fails():
    status_error = RuntimeError("helper final status failed")
    cleanup_error = RuntimeError("helper stderr cleanup failed")

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.returncode = 0
            self.disposal_attempted = False

        def poll(self):
            if self.disposal_attempted:
                raise status_error
            return self.returncode

        def close_stderr(self):
            self.disposal_attempted = True
            raise cleanup_error

    process = Process()
    helper_client = _helper_client()
    helper_client._process = cast(ManagedSshProcess, process)
    helper_client._observations.record_close("process_exit", 0)

    result = helper_client.close()

    assert result.error is status_error
    assert result.cleanup_errors == (cleanup_error,)
    assert process.stdin.closed
    assert process.stdout.closed
    assert any("helper cleanup also failed" in note for note in status_error.__notes__)


def test_close_closes_streams_when_reader_thread_does_not_start(monkeypatch):
    reader_start_error = RuntimeError("helper reader did not start")

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -signal.SIGKILL

        def wait(self, timeout=None):
            del timeout
            return self.returncode

        def close_stderr(self):
            self.stderr.close()

    def fail_start(_thread):
        raise reader_start_error

    process = Process()
    helper_client = _helper_client()
    helper_client._process = cast(ManagedSshProcess, process)
    monkeypatch.setattr(threading.Thread, "start", fail_start)

    result = helper_client.close()

    assert result.error is reader_start_error
    assert result.cleanup_errors == ()
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_close_forces_disposal_after_helper_stop_timeout():
    close_event = encode_message("SESSION_CLOSED", reason="requested", returncode=None)

    class StopInput(io.BytesIO):
        def __init__(self, event_writer: BinaryIO):
            super().__init__()
            self.event_writer = event_writer

        def write(self, payload):
            written = super().write(payload)
            self.event_writer.write(close_event)
            self.event_writer.close()
            return written

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            read_fd, write_fd = os.pipe()
            self.event_writer = os.fdopen(write_fd, "wb", buffering=0)
            self.stdin = StopInput(self.event_writer)
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if timeout is not None and self.returncode is None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -signal.SIGKILL

        @staticmethod
        def stderr_tail():
            return b""

        def close_stderr(self):
            self.stderr.close()

    process = Process()
    helper_client = _helper_client()
    helper_client._process = cast(ManagedSshProcess, process)

    result = helper_client.close()

    assert isinstance(result.error, subprocess.TimeoutExpired)
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_helper_open_retains_nested_cleanup_diagnostics(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    cleanup_error = RuntimeError("helper process cleanup failed")
    cleanup_error.add_note("process cleanup also failed: stream close failed")

    class Process:
        stdout = None

    def fail_stop(_process, *, close_streams=True):
        del close_streams
        raise cleanup_error

    def popen(_self, _host, _remote_command):
        return Process()

    monkeypatch.setattr(SshCommand, "popen", popen)
    monkeypatch.setattr(helper_client_module, "_stop_process", fail_stop)

    with pytest.raises(SessionError) as raised:
        _HelperClient.open(request.ssh_command, request.host, deployment)

    assert raised.value is not cleanup_error
    notes = raised.value.__notes__
    assert any("helper process cleanup failed" in note for note in notes)
    assert any("stream close failed" in note for note in notes)
    assert all("helper startup cleanup also failed" in note for note in notes)


def test_helper_close_keeps_reader_owned_stdout_open_until_reader_stops():
    reader_stopped = threading.Event()

    class ReaderOwnedStream(io.BytesIO):
        def close(self):
            assert reader_stopped.is_set()
            super().close()

    class Reader(threading.Thread):
        @override
        def join(self, timeout=None):
            del timeout

        @override
        def is_alive(self):
            return not reader_stopped.is_set()

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.returncode = None
            self.stdin = io.BytesIO()
            self.stdout = ReaderOwnedStream()
            self.stderr = io.BytesIO()

        def poll(self):
            return self.returncode

        def terminate(self):
            reader_stopped.set()
            self.returncode = 0

        def kill(self):
            reader_stopped.set()
            self.returncode = -signal.SIGKILL

        def wait(self, timeout=None):
            return self.returncode

        def close_stderr(self):
            self.stderr.close()

    helper = _HelperClient(SshCommand(), "host", DeploymentResult("/helper.py", "digest", False))
    process = Process()
    reader = Reader()
    helper._process = cast(ManagedSshProcess, process)
    helper._observations.record_close("process_exit", 0)
    helper._reader_thread = reader

    assert helper.close().error is None

    assert reader_stopped.is_set()
    assert not reader.is_alive()
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_helper_close_retains_nested_process_cleanup_diagnostics():
    terminate_error = RuntimeError("helper terminate failed")
    stderr_error = RuntimeError("helper stderr close failed")

    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            raise terminate_error

        def close_stderr(self):
            raise stderr_error

    helper = _HelperClient(SshCommand(), "host", DeploymentResult("/helper.py", "digest", False))
    helper._process = cast(ManagedSshProcess, Process())
    helper._observations.record_close("requested", None)

    result = helper.close()

    assert isinstance(result.error, SessionError)
    assert result.cleanup_errors == (terminate_error,)
    notes = result.error.__notes__
    assert any("helper terminate failed" in note for note in notes)
    assert any("helper stderr close failed" in note for note in notes)
    assert all("helper cleanup also failed" in note for note in notes)


@pytest.mark.timeout(5)
def test_helper_startup_timeout_does_not_block_on_partial_output(monkeypatch):
    read_fd, write_fd = os.pipe()

    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.stderr = None
            self.returncode = None
            self.args = ("fake-helper",)
            self.terminate_calls = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1
            self.returncode = 0

        def kill(self):
            self.returncode = -signal.SIGKILL

        def wait(self, timeout=None):
            return self.returncode

        @staticmethod
        def stderr_tail():
            return b""

        @staticmethod
        def close_stderr():
            pass

    class Command(_PopenOnlySshCommand):
        @override
        def popen(self, host: str, remote_command: str, *extra_args: str) -> Any:
            return process

    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

    class Selector:
        def __init__(self):
            self.delivered = False

        def register(self, _stream, _events):
            pass

        def select(self, _timeout):
            if not self.delivered:
                self.delivered = True
                clock.now = helper_client_module.HELPER_START_TIMEOUT + 1
                return [(None, None)]
            return []

        @staticmethod
        def close():
            pass

    process = Process()
    clock = Clock()
    try:
        os.write(write_fd, b'{"version":1,"type":"SESSION_CREATED"')
        monkeypatch.setattr(helper_client_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(helper_client_module.selectors, "DefaultSelector", Selector)

        with pytest.raises(SessionError) as raised:
            _HelperClient.open(
                Command(),
                "host",
                DeploymentResult("/helper.py", "digest", False),
            )

        assert isinstance(raised.value.__cause__, TimeoutError)
        assert process.terminate_calls == 1
    finally:
        if not process.stdout.closed:
            process.stdout.close()
        os.close(write_fd)


@pytest.mark.timeout(10)
def test_helper_client_output_delivery_does_not_retain_event_history():
    payloads = [f"payload-{index}" for index in range(1024)]
    frames = [
        encode_message(
            "SESSION_CREATED",
            helper="fake",
            session_id="session",
            remote_workspace="/workspace",
        ),
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        *(
            encode_message(
                "CHILD_OUTPUT",
                stream="stdout" if index % 2 == 0 else "stderr",
                payload=payload,
                line_end=False,
            )
            for index, payload in enumerate(payloads)
        ),
        encode_message("SESSION_CLOSED", reason="process_exit", returncode=0),
    ]

    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            read_fd, write_fd = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)
            self.writer = threading.Thread(
                target=self._write_frames,
                args=(write_fd, b"".join(frames)),
                daemon=True,
            )
            self.writer.start()
            self.stderr = None
            self.returncode = None
            self.args = ("fake-helper",)

        @staticmethod
        def _write_frames(write_fd, frame_bytes):
            try:
                with os.fdopen(write_fd, "wb") as stream:
                    stream.write(frame_bytes)
            except BrokenPipeError:
                pass

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -signal.SIGKILL

        def stderr_tail(self):
            return b""

        def close_stderr(self):
            pass

    class Command(_PopenOnlySshCommand):
        process: Any

        def __init__(self):
            super().__init__()
            object.__setattr__(self, "process", Process())

        @override
        def popen(self, host: str, remote_command: str, *extra_args: str) -> Any:
            return self.process

    handled = []
    command = Command()
    helper_client = _HelperClient.open(
        command,
        "host",
        DeploymentResult("/helper.py", "digest", False),
        output_handler=lambda stream, payload, line_end: handled.append(
            (stream, payload, line_end)
        ),
    )
    try:
        helper_client.start_process(RemoteProcess(("child",)), ())
        assert helper_client._reader_thread is not None
        helper_client._reader_thread.join(timeout=10)
        assert not helper_client._reader_thread.is_alive()
        command.process.writer.join(timeout=10)
        assert not command.process.writer.is_alive()
        assert handled == [
            ("stdout" if index % 2 == 0 else "stderr", payload, False)
            for index, payload in enumerate(payloads)
        ]
        assert helper_client.recorded_openocd_exit() == 0
    finally:
        assert helper_client.close().error is None
