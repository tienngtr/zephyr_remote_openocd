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
from zephyr_remote_openocd.remote.ssh import SshCommand

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


def test_initial_message_rejects_frame_without_lf():
    read_fd, write_fd = os.pipe()
    payload = encode_message(
        "SESSION_CREATED",
        helper="fake",
        session_id="session",
        remote_workspace="/workspace",
    ).rstrip(b"\n")
    os.write(write_fd, payload)
    os.close(write_fd)
    stream = os.fdopen(read_fd, "rb", buffering=0)

    try:
        with pytest.raises(ProtocolError):
            _HelperClient._read_initial_message(stream, helper_client_module.time.monotonic() + 1)
    finally:
        stream.close()


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


def _open_helper_client_with_events(*events: bytes):
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

    class Command(_PopenOnlySshCommand):
        @override
        def popen(self, host: str, remote_command: str, *extra_args: str) -> Any:
            del host, remote_command, extra_args
            return process

    helper_client = _HelperClient.open(
        Command(), "host", DeploymentResult("/helper.py", "digest", False)
    )
    return helper_client, process


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
def test_background_error_ends_session_without_sending_stop():
    helper_client, process = _open_helper_client_with_events(
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        encode_message("ERROR", code="FAILED", message="background failed"),
    )
    helper_client.start_process(RemoteProcess(("child",)), ())

    assert helper_client._reader_thread is not None
    helper_client._reader_thread.join(timeout=5)
    assert not helper_client._reader_thread.is_alive()

    result = helper_client.close()

    assert isinstance(result.error, SessionError)
    assert result.error.__cause__ is None
    commands = [decode_message(bytes(line))["type"] for line in process.stdin.written.splitlines()]
    assert commands == ["START"]


def test_reader_failure_takes_precedence_over_known_openocd_result():
    helper_client = _helper_client()
    helper_client._state.record_close("process_exit", OPENOCD_FAILURE_RC)
    reader_error = RuntimeError("protocol failed")
    helper_client._state.record_reader_failure(reader_error)

    with pytest.raises(SessionError) as raised:
        helper_client.recorded_openocd_exit()

    assert raised.value.__cause__ is reader_error


def test_reader_failure_is_reported_when_helper_exits_without_close_event():
    helper_client = _helper_client()
    reader_error = SessionError("remote helper exited without a session-ending event")
    helper_client._state.record_reader_failure(reader_error)

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
    cast(Any, helper_client)._process = Process()

    result = helper_client.close()

    assert result.error is graceful_stop_error
    assert result.cleanup_errors == (forced_stop_error,)
    assert any("helper cleanup also failed" in note for note in graceful_stop_error.__notes__)


def test_close_reports_helper_stop_timeout():
    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(
                encode_message("SESSION_CLOSED", reason="requested", returncode=None)
            )
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

        @staticmethod
        def close_stderr():
            pass

    helper_client = _helper_client()
    cast(Any, helper_client)._process = Process()

    result = helper_client.close()

    assert isinstance(result.error, subprocess.TimeoutExpired)


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

    class Reader:
        def join(self, timeout=None):
            del timeout

        @staticmethod
        def is_alive():
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
    test_helper = cast(Any, helper)
    test_helper._process = Process()
    test_helper._state.record_close("process_exit", 0)
    test_helper._reader_thread = Reader()

    assert helper.close().error is None

    assert reader_stopped.is_set()
    assert not test_helper._reader_thread.is_alive()
    assert test_helper._process.stdin.closed
    assert test_helper._process.stdout.closed
    assert test_helper._process.stderr.closed
    assert helper.close().error is None


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
    test_helper = cast(Any, helper)
    test_helper._process = Process()
    test_helper._state.request_stop(lambda: None)
    test_helper._state.record_close("requested", None)

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
