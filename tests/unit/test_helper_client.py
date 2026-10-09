# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import io
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast, override

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.remote import helper_client as helper_client_module
from zephyr_remote_openocd.remote.arguments import ArgumentTemplate, SessionValue, TclWord
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.flash import FlashInputs, build_flash_plan
from zephyr_remote_openocd.remote.helper_client import _HelperClient
from zephyr_remote_openocd.remote.model import RemoteProcess
from zephyr_remote_openocd.remote.paths import PathPlanner
from zephyr_remote_openocd.remote.protocol import (
    ProtocolError,
    decode_message,
    encode_message,
    write_start,
)
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, SshCommand, SshLocalForward
from zephyr_remote_openocd.remote_helper import _decode_start, materialize_argv, materialize_path

OPENOCD_FAILURE_RC = 7

# These tests intentionally retain narrow typing escapes at the process-control
# boundary.  _HelperClient owns the complete ManagedSshProcess lifecycle, so a
# smaller production protocol would exist only for tests.  The process-shaped
# doubles below inject behavior a real wrapper cannot express deterministically:
# poll and wait failures, STOP/write races, termination and stderr-close
# failures, reader shutdown ordering, and partial event-stream delivery.  Tests
# that do not need those faults use real ManagedSshProcess construction in the
# local-integration layer.


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


class _ControlProcess:
    """Memory-backed shutdown transport; fault tests override the failing operation."""

    args = ("fake-helper",)

    def __init__(self) -> None:
        self.stdin: BinaryIO = io.BytesIO()
        self.stdout: BinaryIO = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
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


def _open_helper_client(
    process: _EventProcess,
    *,
    process_start_handler: Callable[[tuple[str, ...]], None] | None = None,
) -> _HelperClient:
    class Command(_PopenOnlySshCommand):
        @override
        def popen(
            self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
        ) -> Any:
            del host, remote_command, local_forward
            return process

    client = _HelperClient(
        Command(),
        "host",
        DeploymentResult("/helper.py", "digest", False),
        process_start_handler=process_start_handler,
    )
    try:
        client.acquire()
    except BaseException:
        client.close()
        raise
    return client


def _open_helper_client_with_events(
    *events: bytes,
    process_start_handler: Callable[[tuple[str, ...]], None] | None = None,
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
    return _open_helper_client(process, process_start_handler=process_start_handler), process


@pytest.fixture
def helper_client() -> Iterator[_HelperClient]:
    client, process = _open_helper_client_with_events()
    try:
        yield client
    finally:
        # Fault tests may replace the transport; release the acquisition
        # transport's pipes independently of the injected shutdown behavior.
        process.stdin.close()
        process.stdout.close()
        process.close_stderr()


def test_helper_client_sends_preference_and_reports_attempts_before_startup_error():
    observed: list[tuple[str, ...]] = []
    first = ("openocd", "-c", "bindto 127.64.0.1", "")
    retry = ("openocd", "-c", "bindto 127.64.0.2", "")
    client, helper_process = _open_helper_client_with_events(
        encode_message("PROCESS_STARTING", argv=list(first)),
        encode_message("CHILD_OUTPUT", stream="stderr", payload="bind collision", line_end=True),
        encode_message("PROCESS_STARTING", argv=list(retry)),
        encode_message("ERROR", code="FAILED", message="startup failed"),
        process_start_handler=observed.append,
    )
    try:
        with pytest.raises(SessionError):
            client.start_process(RemoteProcess(("openocd",)), (), preferred_address="127.64.0.1")
        request = _decode_start(decode_message(bytes(helper_process.stdin.written)))
        assert request.preferred_address == "127.64.0.1"
        assert observed == [first, retry]
    finally:
        client.close()


def test_explicit_session_values_are_not_recursively_expanded():
    process = RemoteProcess(
        ("openocd", "preview"),
        argv_templates=(
            (
                1,
                ArgumentTemplate(
                    (
                        "load_image ",
                        TclWord(
                            (
                                SessionValue.WORKSPACE,
                                "/",
                                SessionValue.ADDRESS,
                                "/{workspace}/{address}/firmware.bin",
                            )
                        ),
                        " 0x1000",
                    )
                ),
            ),
        ),
    )
    stream = io.BytesIO()
    write_start(stream, process, ())
    request = _decode_start(decode_message(stream.getvalue()))
    argv = materialize_argv(
        request.argv,
        workspace=r'/runtime/[review] $value "quoted" \backslash {workspace} {address}/session',
        address="127.64.0.1",
        argv_templates=request.argv_templates,
    )
    assert argv[1] == (
        r'load_image "/runtime/\[review\] \$value \"quoted\" \\backslash '
        r'\{workspace\} \{address\}/session/127.64.0.1/'
        r'\{workspace\}/\{address\}/firmware.bin" 0x1000'
    )


@pytest.mark.parametrize("image_type", ("hex", "bin", "elf"))
@pytest.mark.parametrize(
    "workspace, mapped_root, expected_image_root",
    (
        (
            r'/runtime/[review] $value "quoted" \backslash {workspace} {address}/session',
            None,
            r'/runtime/\[review\] \$value \"quoted\" \\backslash '
            r'\{workspace\} \{address\}/session/staged/files',
        ),
        ("/workspace", "/shared/{address}", r"/shared/\{address\}"),
        ("/workspace", "/shared/{workspace}", r"/shared/\{workspace\}"),
    ),
)
def test_flash_paths_are_quoted_after_session_allocation(
    tmp_path: Path,
    image_type: str,
    workspace: str,
    mapped_root: str | None,
    expected_image_root: str,
):
    image = tmp_path / f"firmware.{image_type}"
    if image_type == "elf":
        shutil.copyfile(sys.executable, image)
    else:
        image.write_bytes(b":00000001FF\n")
    config = tmp_path / "board.cfg"
    config.write_text("# fixture\n")
    config_remote = r'/configs/[review] $value "quoted" \backslash {workspace} {address}/board.cfg'
    executable = r'/tools/[review] $value "quoted" \backslash {workspace} {address}/openocd'
    user_tcl = 'puts "user [expr {1 + 2}] $value {workspace} {address}"'
    mappings: tuple[PathMapping, ...] = (PathMapping(config, PurePosixPath(config_remote)),)
    if mapped_root is not None:
        mappings += (PathMapping(tmp_path, PurePosixPath(mapped_root)),)
    plan = build_flash_plan(
        FlashInputs(
            executable=executable,
            image_type=image_type,
            file=str(image),
            elf_file=None,
            hex_file=None,
            bin_file=None,
            search_paths=(),
            config_files=(str(config),),
            pre_init=(user_tcl,),
            load_command="load_image",
            verify_command="verify_image",
            flash_address="0x1000",
            verify=True,
        ),
        PathPlanner(mappings),
    )
    process = _EventProcess(
        (
            encode_message(
                "SESSION_CREATED", helper="fake", session_id="session", remote_workspace=workspace
            ),
            encode_message("ERROR", code="FAILED", message="controlled startup failure"),
        )
    )
    client = _open_helper_client(process)
    try:
        with pytest.raises(SessionError):
            client.start_process(plan.process, ())
        request = _decode_start(decode_message(bytes(process.stdin.written).splitlines()[0]))
        argv = materialize_argv(
            request.argv,
            workspace=workspace,
            address="127.64.0.1",
            literal_prefix=request.literal_prefix,
            argv_templates=request.argv_templates,
        )
    finally:
        client.close()

    assert argv[0] == executable
    assert argv[argv.index("-f") + 1] == config_remote
    assert "bindto 127.64.0.1" in argv
    if mapped_root is not None:
        checked_paths = tuple(
            materialize_path(check.path, workspace=workspace, address="127.64.0.1")
            for check in request.required_paths
        )
        assert f"{mapped_root}/firmware.{image_type}" in checked_paths
    assert user_tcl in argv
    commands = [
        argv[index + 1]
        for index, argument in enumerate(argv[:-1])
        if argument == "-c" and argv[index + 1].startswith(("load_image ", "verify_image "))
    ]
    suffix = " 0x1000" if image_type == "bin" else ""
    assert commands == [
        f'load_image "{expected_image_root}/firmware.{image_type}"{suffix}',
        f'verify_image "{expected_image_root}/firmware.{image_type}"{suffix}',
    ]


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
def test_recorded_openocd_exit_is_available_while_reader_remains_alive(helper_client):
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


def test_close_waits_when_process_exit_wins_stop_race(monkeypatch, helper_client):
    natural_exit_reaped = threading.Event()

    class Process(_ControlProcess):
        @override
        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                # Forced termination must not stand in for reaping the natural exit.
                natural_exit_reaped.set()
            return super().wait(timeout)

    class Reader(threading.Thread):
        @override
        def join(self, timeout=None):
            del timeout

        @override
        def is_alive(self):
            return False

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
    assert natural_exit_reaped.is_set()
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_large_start_is_rejected_before_helper_write():
    helper_client, process = _open_helper_client_with_events(
        encode_message("SESSION_CLOSED", reason="requested", returncode=None),
    )
    # JSON escaping makes the encoded frame oversized even though its argv
    # contains fewer than 1 MiB of UTF-8 bytes.
    request = RemoteProcess(("child", "é" * 200_000))
    try:
        with pytest.raises(ProtocolError):
            helper_client.start_process(request, ())
        assert process.stdin.written == b""
    finally:
        helper_client.close()


def test_startup_error_ends_session_without_stop_or_missing_close_failure():
    helper_client, process = _open_helper_client_with_events(
        encode_message("ERROR", code="FAILED", message="startup failed"),
    )

    with pytest.raises(SessionError, match="startup failed"):
        helper_client.start_process(RemoteProcess(("child",)), ())

    result = helper_client.close()

    assert result.error is None
    commands = [decode_message(bytes(line))["type"] for line in process.stdin.written.splitlines()]
    assert commands == ["START"]


@pytest.mark.timeout(10)
def test_unexpected_requested_close_is_reported_by_active_operation_result():
    helper_client, _process = _open_helper_client_with_events(
        encode_message("PROCESS_STARTING", argv=["child"]),
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        encode_message("SESSION_CLOSED", reason="requested", returncode=None),
    )
    helper_client.start_process(RemoteProcess(("child",)), ())
    helper_client.wait_for_change(5)

    try:
        with pytest.raises(SessionError) as raised:
            helper_client.recorded_openocd_exit()

        assert raised.value.__cause__ is None
    finally:
        helper_client.close()


@pytest.mark.timeout(10)
def test_observed_background_error_is_not_reported_again_on_close():
    helper_client, process = _open_helper_client_with_events(
        encode_message("PROCESS_STARTING", argv=["child"]),
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


@pytest.mark.parametrize("cleanup_fails", (False, True), ids=("clean", "cleanup-failure"))
@pytest.mark.timeout(10)
def test_close_reports_background_error_without_stop(monkeypatch, cleanup_fails):
    helper_client, process = _open_helper_client_with_events(
        encode_message("PROCESS_STARTING", argv=["child"]),
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        encode_message("ERROR", code="FAILED", message="background failed"),
    )
    helper_client.start_process(RemoteProcess(("child",)), ())
    helper_client.wait_for_change(5)

    cleanup_error = RuntimeError("forced cleanup failed")

    def fail_stop(_process, *, close_streams=True):
        del close_streams
        raise cleanup_error

    if cleanup_fails:
        monkeypatch.setattr(helper_client_module, "_stop_process", fail_stop)

    result = helper_client.close()

    assert isinstance(result.error, SessionError)
    assert "background failed" in str(result.error)
    assert result.error.__cause__ is None
    assert result.cleanup_errors == ((cleanup_error,) if cleanup_fails else ())
    if cleanup_fails:
        assert any(str(cleanup_error) in note for note in result.error.__notes__)
    commands = [decode_message(bytes(line))["type"] for line in process.stdin.written.splitlines()]
    assert commands == ["START"]


def test_reader_failure_takes_precedence_over_known_openocd_result(helper_client):
    helper_client._observations.record_close("process_exit", OPENOCD_FAILURE_RC)
    reader_error = RuntimeError("protocol failed")
    helper_client._observations.record_reader_failure(reader_error)

    assert helper_client.openocd_returncode == OPENOCD_FAILURE_RC
    with pytest.raises(SessionError) as raised:
        helper_client.recorded_openocd_exit()

    assert raised.value.__cause__ is reader_error


def test_close_keeps_stop_failure_primary_when_forced_cleanup_also_fails(helper_client):
    graceful_stop_error = RuntimeError("graceful stop failed")
    forced_stop_error = RuntimeError("forced stop failed")

    class FailingStdin(io.BytesIO):
        def write(self, _payload):
            raise graceful_stop_error

    class Process(_ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdin = FailingStdin()

        @override
        def terminate(self) -> None:
            raise forced_stop_error

    helper_client._process = cast(ManagedSshProcess, Process())

    result = helper_client.close()

    assert result.error is graceful_stop_error
    assert result.cleanup_errors == (forced_stop_error,)
    assert any(str(forced_stop_error) in note for note in graceful_stop_error.__notes__)


def test_close_cleans_up_helper_when_initial_status_observation_fails(helper_client):
    observation_error = RuntimeError("helper status failed")

    class Process(_ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.initial_status_failure_pending = True

        @override
        def poll(self) -> int | None:
            if self.initial_status_failure_pending:
                self.initial_status_failure_pending = False
                raise observation_error
            return self.returncode

    process = Process()
    helper_client._process = cast(ManagedSshProcess, process)

    result = helper_client.close()

    assert result.error is observation_error
    assert result.cleanup_errors == ()
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_close_cleans_up_helper_when_reader_join_fails(helper_client):
    join_error = RuntimeError("helper reader join failed")

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

    process = _ControlProcess()
    process.returncode = 0
    helper_client._process = cast(ManagedSshProcess, process)
    helper_client._reader_thread = Reader()
    helper_client._observations.record_close("process_exit", 0)

    result = helper_client.close()

    assert result.error is None
    assert result.cleanup_errors == (join_error,)
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_close_preserves_cleanup_error_when_final_status_observation_fails(helper_client):
    status_error = RuntimeError("helper final status failed")
    cleanup_error = RuntimeError("helper stderr cleanup failed")

    class Process(_ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = 0
            self.cleanup_attempted = False

        @override
        def poll(self) -> int | None:
            if self.cleanup_attempted:
                raise status_error
            return self.returncode

        @override
        def close_stderr(self) -> None:
            self.cleanup_attempted = True
            raise cleanup_error

    process = Process()
    helper_client._process = cast(ManagedSshProcess, process)
    helper_client._observations.record_close("process_exit", 0)

    result = helper_client.close()

    assert result.error is status_error
    assert result.cleanup_errors == (cleanup_error,)
    assert process.stdin.closed
    assert process.stdout.closed
    assert any(str(cleanup_error) in note for note in status_error.__notes__)


def test_close_closes_streams_when_reader_thread_does_not_start(monkeypatch, helper_client):
    reader_start_error = RuntimeError("helper reader did not start")

    def fail_start(_thread):
        raise reader_start_error

    process = _ControlProcess()
    helper_client._process = cast(ManagedSshProcess, process)
    monkeypatch.setattr(threading.Thread, "start", fail_start)

    result = helper_client.close()

    assert result.error is reader_start_error
    assert result.cleanup_errors == ()
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize(
    ("wait_failure_type", "stdin_close_fails"),
    (
        pytest.param(subprocess.TimeoutExpired, False, id="timeout"),
        pytest.param(asyncio.CancelledError, False, id="cancelled"),
        pytest.param(KeyboardInterrupt, False, id="interrupted"),
        pytest.param(subprocess.TimeoutExpired, True, id="timeout-with-stdin-close-error"),
    ),
)
def test_close_forces_cleanup_after_helper_wait_failure(
    helper_client, wait_failure_type, stdin_close_fails
):
    close_event = encode_message("SESSION_CLOSED", reason="requested", returncode=None)
    stdin_close_error = OSError("helper stdin close failed")
    wait_error = (
        subprocess.TimeoutExpired(("fake-helper",), helper_client_module.HELPER_STOP_TIMEOUT)
        if wait_failure_type is subprocess.TimeoutExpired
        else wait_failure_type()
    )

    class StopInput(io.BytesIO):
        def __init__(self, event_writer: BinaryIO):
            super().__init__()
            self.event_writer = event_writer

        def write(self, payload):
            written = super().write(payload)
            self.event_writer.write(close_event)
            self.event_writer.close()
            return written

        def close(self):
            super().close()
            if stdin_close_fails:
                raise stdin_close_error

    class Process(_ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            read_fd, write_fd = os.pipe()
            self.event_writer = os.fdopen(write_fd, "wb", buffering=0)
            self.stdin = StopInput(self.event_writer)
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)

        @override
        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None and self.returncode is None:
                raise wait_error
            return super().wait(timeout)

    process = Process()
    helper_client._process = cast(ManagedSshProcess, process)

    result = helper_client.close()

    assert result.error is wait_error
    assert result.cleanup_errors == ((stdin_close_error,) if stdin_close_fails else ())
    if stdin_close_fails:
        assert any(str(stdin_close_error) in note for note in wait_error.__notes__)
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_helper_close_keeps_reader_owned_stdout_open_until_reader_stops(helper_client):
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

    class Process(_ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdout = ReaderOwnedStream()

        @override
        def wait(self, timeout: float | None = None) -> int:
            returncode = super().wait(timeout)
            reader_stopped.set()
            return returncode

    helper = helper_client
    process = Process()
    reader = Reader()
    helper._process = cast(ManagedSshProcess, process)
    helper._observations.record_close("process_exit", 0)
    helper._reader_thread = reader

    result = helper.close()

    assert result.error is None
    assert result.cleanup_errors == ()

    assert reader_stopped.is_set()
    assert not reader.is_alive()
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_helper_close_retains_nested_process_cleanup_diagnostics(helper_client):
    terminate_error = RuntimeError("helper terminate failed")
    stderr_error = RuntimeError("helper stderr close failed")

    class Process(_ControlProcess):
        @override
        def terminate(self) -> None:
            super().terminate()
            raise terminate_error

        @override
        def close_stderr(self) -> None:
            raise stderr_error

    helper = helper_client
    helper._process = cast(ManagedSshProcess, Process())
    helper._observations.record_close("requested", None)

    result = helper.close()

    assert isinstance(result.error, SessionError)
    assert result.cleanup_errors == (terminate_error,)
    notes = result.error.__notes__
    assert any("helper terminate failed" in note for note in notes)
    assert any("helper stderr close failed" in note for note in notes)


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
        def popen(
            self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
        ) -> Any:
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

        client = _HelperClient(Command(), "host", DeploymentResult("/helper.py", "digest", False))
        with pytest.raises(SessionError) as raised:
            try:
                client.acquire()
            finally:
                assert client.close().cleanup_errors == ()

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
        encode_message("PROCESS_STARTING", argv=["child"]),
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
        def popen(
            self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
        ) -> Any:
            return self.process

    handled = []
    command = Command()
    helper_client = _HelperClient(
        command,
        "host",
        DeploymentResult("/helper.py", "digest", False),
        output_handler=lambda stream, payload, line_end: handled.append(
            (stream, payload, line_end)
        ),
    )
    try:
        helper_client.acquire()
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
