# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

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
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast, override
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote import rtt as rtt_module
from zephyr_remote_openocd.remote.backend import (
    RemoteSession,
    query_remote_openocd_version,
)
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    StagedFile,
)
from zephyr_remote_openocd.remote.paths import ADDRESS_TOKEN, PathPlanner
from zephyr_remote_openocd.remote.protocol import (
    EventOrder,
    encode_message,
)
from zephyr_remote_openocd.remote.rtt import RttClientError, run_rtt_client
from zephyr_remote_openocd.remote.services import (
    LOOPBACK_RANGE,
)
from zephyr_remote_openocd.remote.session import (
    SessionError,
)
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
    readiness_marker=None,
    readiness_timeout=30.0,
    literal_prefix=0,
):
    return encode_message(
        "START",
        argv=list(argv),
        environment={} if environment is None else environment,
        required_paths=[] if required_paths is None else required_paths,
        services=list(services),
        readiness_marker=readiness_marker,
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
            self.returncode = -9

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
        session = cast(Any, object.__new__(RemoteSession))
        session.request = RemoteSessionRequest("target", command, TEST_PROCESS)
        session.forwards = []
        session.closed = False
        session.output_handler = None
        session._openocd_returncode = None
        session.reader_error = None
        session.reader_thread = None
        session._order = EventOrder()
        session._terminal_reason = None
        session._state_lock = threading.RLock()
        session._state_changed = threading.Condition(session._state_lock)
        return session

    @staticmethod
    def helper_process():
        process = TestForwardingLifecycle.Process()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO(
            encode_message("SESSION_CLOSED", reason="requested", returncode=None)
        )
        return process

    @staticmethod
    def port():
        try:
            listener = socket.socket()
        except PermissionError:
            pytest.skip("sandbox prohibits loopback listeners")
        with listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def test_gdb_listener_readiness_does_not_probe_single_client_socket(self):
        process = self.Process()
        command = self.Command(process)
        session = self.session(command)
        service = Service("gdb", self.port(), 3333)
        with (
            patch.object(RemoteSession, "_await_forward_ready", return_value=True),
            patch("zephyr_remote_openocd.remote.backend.socket.create_connection") as connect,
        ):
            session._start_forwards((service,), "127.64.1.1")
        connect.assert_not_called()
        remote_command = command.calls[0][1]
        assert remote_command.startswith("python3 -c ")
        assert "-N" not in command.calls[0][2]
        session._close_forwards()
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
        with patch.object(RemoteSession, "_read_event", side_effect=events):
            _opened_session(
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
            patch.object(RemoteSession, "_read_event", side_effect=startup_error),
            patch.object(
                RemoteSession,
                "_stop_process",
                side_effect=RuntimeError("process cleanup failed"),
            ),
            pytest.raises(SessionError, match="invalid helper response") as raised,
        ):
            _opened_session(
                RemoteSessionRequest("target", command, TEST_PROCESS),
                DeploymentResult("/helper.py", "digest", False),
            )

        assert raised.value is startup_error
        assert any("process cleanup failed" in note for note in raised.value.__notes__)

    def test_wait_error_leaves_cleanup_to_lifecycle(self):
        session = self.session(self.Command(self.Process()))
        wait_error = SessionError("event stream failed")

        with (
            patch.object(session, "check_openocd_exit", side_effect=wait_error),
            patch.object(session, "close") as close,
            pytest.raises(SessionError) as raised,
        ):
            session.wait_for_openocd_exit()

        assert raised.value is wait_error
        close.assert_not_called()

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
                clock.now = backend_module.FORWARD_START_TIMEOUT + 1
                return []

            @staticmethod
            def close():
                pass

        clock = Clock()
        monkeypatch.setattr(backend_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(backend_module.selectors, "DefaultSelector", Selector)
        stale = self.Process()
        read_fd, write_fd = os.pipe()
        stale.stdout = os.fdopen(read_fd, "rb")
        command = self.Command(stale)
        session = self.session(command)
        service = Service("gdb", 32155, 3333)
        monkeypatch.setattr(
            RemoteSession,
            "_preflight",
            staticmethod(lambda _service: "stale listener"),
        )
        try:
            with pytest.raises(SessionError, match="did not become ready"):
                session._start_forwards((service,), "127.64.1.1")
        finally:
            os.close(write_fd)
        session._close_forwards()
        assert stale.terminate_calls == 1

    def test_forward_cleanup_closes_all_forwards_idempotently(self):
        first = self.Process()
        second = self.Process()
        session = self.session(self.Command(first))
        session.forwards = [first, second]
        session._close_forwards()
        session._close_forwards()
        assert second.terminate_calls == 1
        assert first.terminate_calls == 1

    def test_close_attempts_helper_and_all_forwards_after_cleanup_failure(self):
        class FailingProcess(TestForwardingLifecycle.Process):
            def __init__(self):
                super().__init__()
                self.fail_termination = True

            def terminate(self):
                self.terminate_calls += 1
                if self.fail_termination:
                    raise RuntimeError("forward cleanup failed")
                self.returncode = 0

        failed = FailingProcess()
        healthy = self.Process()
        helper = self.helper_process()
        session = self.session(self.Command(helper))
        session.helper_process = helper
        session.forwards = [failed, healthy]

        with pytest.raises(RuntimeError, match="forward cleanup failed"):
            session.close()

        assert healthy.terminate_calls == 1
        assert helper.terminate_calls == 1
        assert session.forwards == []
        assert session.closed

        session.close()
        assert failed.terminate_calls == 1

    def test_close_forces_helper_after_unexpected_graceful_stop_failure(self):
        class FailingStdin:
            def __init__(self):
                self.closed = False
                self.fail = True

            def write(self, _payload):
                if self.fail:
                    raise RuntimeError("graceful stop failed")

            def flush(self):
                pass

            def close(self):
                self.closed = True

        class FailingHelper(TestForwardingLifecycle.Process):
            def __init__(self):
                super().__init__()
                self.fail_termination = True

            def terminate(self):
                self.terminate_calls += 1
                if self.fail_termination:
                    raise RuntimeError("forced stop failed")
                self.returncode = 0

        helper = FailingHelper()
        helper.stdout = io.BytesIO(
            encode_message("SESSION_CLOSED", reason="requested", returncode=None)
        )
        helper.stdin = FailingStdin()
        session = self.session(self.Command(helper))
        session.helper_process = helper

        with pytest.raises(RuntimeError, match="graceful stop failed") as raised:
            session.close()

        assert helper.terminate_calls == 1
        assert any("forced stop failed" in note for note in raised.value.__notes__)
        assert session.closed

        helper.stdin.fail = False
        helper.fail_termination = False
        session.close()
        assert helper.terminate_calls == 1
        assert session.closed

    def test_close_reports_helper_cleanup_timeout(self):
        class StuckHelper(TestForwardingLifecycle.Process):
            def __init__(self):
                super().__init__()
                self.stdin = io.BytesIO()

            def wait(self, timeout=None):
                if self.returncode is None:
                    raise subprocess.TimeoutExpired("helper", timeout)
                return self.returncode

        helper = StuckHelper()
        helper.stdout = io.BytesIO(
            encode_message("SESSION_CLOSED", reason="requested", returncode=None)
        )
        session = self.session(self.Command(helper))
        session.helper_process = helper

        with pytest.raises(subprocess.TimeoutExpired):
            session.close()

        assert helper.terminate_calls == 1
        assert session.closed

    @pytest.mark.parametrize("returncode", (0, OPENOCD_FAILURE_RC))
    @pytest.mark.timeout(10)
    def test_check_openocd_exit_reports_consumed_status_while_helper_remains_alive(
        self, returncode
    ):
        session = self.session(self.Command(self.Process()))
        session.helper_process = session.request.ssh_command.process
        session._openocd_returncode = None
        session.reader_error = None
        event_consumed = threading.Event()
        release_reader = threading.Event()

        def consume_session_closed():
            session._dispatch(
                {"type": "SESSION_CLOSED", "reason": "process_exit", "returncode": returncode}
            )
            event_consumed.set()
            release_reader.wait()

        session.reader_thread = threading.Thread(target=consume_session_closed)
        session.reader_thread.start()
        event_consumed.wait()
        try:
            assert session.reader_thread.is_alive()
            assert session.helper_process.poll() is None
            assert session.check_openocd_exit() == returncode
        finally:
            release_reader.set()
            session.reader_thread.join()

    def test_check_openocd_exit_preserves_reader_error_before_known_process_exit(self):
        session = self.session(self.Command(self.Process()))
        session.helper_process = session.request.ssh_command.process
        session._openocd_returncode = OPENOCD_FAILURE_RC
        session.reader_error = RuntimeError("protocol failed")

        with pytest.raises(SessionError, match="helper event stream failed: protocol failed"):
            session.check_openocd_exit()

    def test_check_preserves_reader_recorded_helper_exit(self):
        session = self.session(self.Command(self.Process(returncode=HELPER_FAILURE_RC)))
        session.helper_process = session.request.ssh_command.process
        session._openocd_returncode = None
        session.reader_error = SessionError(f"remote helper exited with status {HELPER_FAILURE_RC}")
        session.reader_thread = None

        with pytest.raises(
            SessionError,
            match=rf"remote helper exited with status {HELPER_FAILURE_RC}",
        ):
            session.check_openocd_exit()


class TestRttClient:
    @staticmethod
    def _listener(handler):
        try:
            listener = socket.socket()
        except PermissionError:
            pytest.skip("sandbox prohibits loopback listeners")
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        thread = threading.Thread(target=handler, args=(listener,), daemon=True)
        thread.start()
        return listener.getsockname()[1], thread

    def test_bidirectional_non_tty_channel(self):
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
            nonlocal input_resumed
            if not connection.sent:
                if _writable:
                    return [], [connection], []
                return [input_fd], [], []
            input_resumed = True
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
                ) as select_call,
            ):
                assert run_rtt_client(5555, poll_session, stdin=stdin, stdout=stdout) == 0

        assert input_forwarded_after_resume
        assert connection.sent == [b"abcd", b"cdef"]
        assert [call.args[:2] for call in select_call.call_args_list] == [
            ((input_fd, connection), ()),
            ((connection,), (connection,)),
            ((input_fd, connection), (connection,)),
        ]

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
            pytest.raises(
                RttClientError,
                match="RTT channel closed while remote session is still running",
            ),
        ):
            run_rtt_client(5555, lambda: None, stdin=stream, stdout=stream)

    def test_eof_drains_pending_session_closed_status(self):
        class Connection:
            def recv(self, _size):
                session._dispatch(
                    {"type": "SESSION_CLOSED", "reason": "process_exit", "returncode": 0}
                )
                return b""

            def close(self):
                pass

        session = TestForwardingLifecycle.session(
            TestForwardingLifecycle.Command(TestForwardingLifecycle.Process())
        )
        session.helper_process = session.request.ssh_command.process
        session._openocd_returncode = None
        session.reader_error = None
        connection = Connection()
        with (
            tempfile.TemporaryFile("w+b") as stream,
            patch.object(rtt_module, "_connect", return_value=(connection, b"connected")),
            patch.object(rtt_module.select, "select", return_value=([connection], [], [])),
        ):
            assert (
                run_rtt_client(
                    5555,
                    session.check_openocd_exit,
                    stdin=stream,
                    stdout=stream,
                )
                == 0
            )

    def test_immediate_forwarded_channel_failure_is_authoritative(self):
        def server(listener):
            with listener, listener.accept()[0]:
                pass

        port, thread = self._listener(server)
        with pytest.raises(RttClientError, match="remote channel"):
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
            session = cast(Any, object.__new__(RemoteSession))
            session.request = RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS)
            session.deployment = DeploymentResult("/helper.py", "0" * 64, False)
            session.allocation = SessionAllocation("session", "/workspace")
            with pytest.raises(SessionError, match="invalid remote staging response"):
                session._stage((StagedFile(source, PurePosixPath("firmware.bin")),))

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
            lambda _command, _host: DeploymentResult("/helper.py", "0" * 64, False),
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

    def test_helper_rejects_malformed_and_unsupported_version(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        for frame in (b"not-json\n", b'{"version":2,"type":"STOP"}\n'):
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

    def test_persistent_process_requires_marker_and_connectable_service(self):
        try:
            probe = socket.socket()
            probe.bind(("127.64.0.1", 0))
            remote_port = probe.getsockname()[1]
            probe.close()
        except PermissionError:
            pytest.skip("sandbox prohibits loopback listeners")
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
                marker = "ZRO_READY_unit"
                child_code = (
                    "import socket,sys,time;"
                    "s=socket.socket();s.bind((sys.argv[1],int(sys.argv[2])));s.listen();"
                    "print(sys.argv[3],flush=True);time.sleep(30)"
                )
                process.stdin.write(
                    start_frame(
                        [
                            sys.executable,
                            "-c",
                            child_code,
                            ADDRESS_TOKEN,
                            str(remote_port),
                            marker,
                        ],
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        readiness_marker=marker,
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
                    event["type"] == "CHILD_OUTPUT" and event["payload"] == marker
                    for event in events
                )
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

    def test_helper_retries_an_openocd_address_collision(self):
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
                try:
                    port_socket = socket.socket()
                except PermissionError:
                    pytest.skip("sandbox prohibits loopback listeners")
                port_socket.bind(("127.0.0.1", 0))
                remote_port = port_socket.getsockname()[1]
                port_socket.close()
                marker = "ZRO_READY_collision"
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
                            ADDRESS_TOKEN,
                            str(remote_port),
                            marker,
                        ],
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        environment={"ZRO_COLLISION_STATE": str(state)},
                        readiness_marker=marker,
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
                    'import sys;print("out");print("err",file=sys.stderr);sys.exit(7)',
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
                assert exit_event["returncode"] == 7
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
                try:
                    listener = socket.socket()
                except PermissionError:
                    pytest.skip("sandbox prohibits loopback listeners")
                with listener:
                    listener.bind(("127.0.0.1", 0))
                    remote_port = listener.getsockname()[1]
                marker = "ZRO_READY_partial"
                command = [
                    sys.executable,
                    "-c",
                    "import sys,time; print(sys.argv[1], flush=True); time.sleep(30)",
                    marker,
                ]
                process.stdin.write(
                    start_frame(
                        command,
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        readiness_marker=marker,
                        readiness_timeout=0.5,
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
            remote_process = RemoteProcess(
                (sys.executable, "-c", 'import sys;print("hello");sys.exit(6)'),
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
                assert backend.wait_for_openocd_exit(5) == 6
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
                readiness_marker="ZRO_DESCENDANT_READY",
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
        ("terminal", "exit_code", "expected"),
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
            ),
            (
                '{"version":1,"type":"SESSION_CLOSED","reason":"requested",'
                '"returncode":null}\n'
                '{"version":1,"type":"ERROR","code":"CLEANUP",'
                '"message":"cleanup failed"}',
                0,
                "helper event stream failed: unexpected ERROR event in closed state",
            ),
            (
                '{"version":1,"type":"SESSION_CLOSED","reason":"requested",'
                '"returncode":null}\nnot-json',
                0,
                "helper event stream failed: malformed protocol message",
            ),
            (
                json.dumps(
                    {"version": 1, "type": "ERROR", "code": "CLEANUP", "message": "cleanup failed"},
                    separators=(",", ":"),
                ),
                7,
                "remote helper error: cleanup failed",
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
                "remote helper exited with status 7 after requested shutdown",
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
                "invalid required fields for SESSION_CLOSED",
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
                "remote helper exited with status 7 after process_exit shutdown",
            ),
            (None, 7, "did not produce SESSION_CLOSED"),
        ),
        ids=(
            "requested-success",
            "process-exit-after-stop-success",
            "requested-then-error",
            "requested-then-malformed",
            "error-and-nonzero",
            "requested-terminal-nonzero",
            "malformed-terminal",
            "process-exit-with-nonzero-helper",
            "nonzero-without-terminal",
        ),
    )
    def test_backend_close_validates_helper_shutdown(self, terminal, exit_code, expected):
        terminal_line = "" if terminal is None else terminal + "\n"
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
sys.stdout.write({terminal_line!r})
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

        backend = _opened_session(
            RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )
        backend._start_event_drain()
        try:
            if expected is None:
                assert backend.close() is None
                assert backend.closed
                assert backend._terminal_reason in {"requested", "process_exit"}
                if backend._terminal_reason == "process_exit":
                    assert backend._openocd_returncode == 7
                else:
                    assert backend._openocd_returncode is None
            else:
                with pytest.raises(SessionError, match=expected) as raised:
                    backend.close()
                assert raised.value
                assert backend.closed
                backend.close()
        finally:
            with suppress(BaseException):
                backend.close()

    def test_backend_rejects_requested_terminal_before_local_stop(self):
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

        backend = _opened_session(
            RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )
        terminal_consumed = threading.Event()
        dispatch = backend._dispatch

        def observe_terminal(event):
            dispatch(event)
            if event["type"] == "SESSION_CLOSED":
                terminal_consumed.set()

        with patch.object(backend, "_dispatch", side_effect=observe_terminal):
            backend._start_event_drain()
            assert backend.reader_thread is not None
            assert terminal_consumed.wait(5)
        assert backend._terminal_reason == "requested"
        try:
            with pytest.raises(SessionError, match="before STOP"):
                backend.close()
            assert backend.closed
        finally:
            with suppress(BaseException):
                backend.close()

    def test_backend_close_preserves_first_forward_failure_and_notes_helper_failure(self):
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
sys.stdin.buffer.readline()
print(
    json.dumps(
        {"version": 1, "type": "ERROR", "code": "CLEANUP", "message": "cleanup failed"}
    ),
    flush=True,
)
sys.exit(7)
"""

        class LocalCommand(_BlockedSshCommand):
            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                return managed_popen(
                    [sys.executable, "-c", helper_code],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

        class FailingForward:
            def __init__(self):
                self.returncode = None
                self.terminate_calls = 0
                self.fail_termination = True
                self.stdin = None
                self.stdout = None
                self.stderr = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1
                if self.fail_termination:
                    raise RuntimeError("forward cleanup failed")
                self.returncode = 0

            def kill(self):
                raise RuntimeError("forward cleanup failed")

            def wait(self, timeout=None):  # pylint: disable=unused-argument
                return self.returncode

        backend = _opened_session(
            RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )
        forward = FailingForward()
        backend.forwards = [cast(Any, forward)]
        backend._start_event_drain()
        try:
            with pytest.raises(RuntimeError, match="forward cleanup failed") as raised:
                backend.close()
            assert any("cleanup failed" in note for note in raised.value.__notes__)
            assert backend.forwards == []
            assert backend.closed

            forward.fail_termination = False
            backend.close()
            assert backend.forwards == []
            assert backend.closed
        finally:
            with suppress(BaseException):
                backend.close()

    def test_backend_reports_helper_failure_after_close_event(self):
        helper_code = """
import json
import sys

events = (
    {
        "version": 1,
        "type": "SESSION_CREATED",
        "helper": "test",
        "session_id": "id",
        "remote_workspace": "/workspace",
    },
    {"version": 1, "type": "PROCESS_READY", "remote_address": "127.64.0.1", "child_pid": 1},
    {"version": 1, "type": "SESSION_CLOSED", "reason": "process_exit", "returncode": 0},
)
for event in events:
    print(json.dumps(event, separators=(",", ":")), flush=True)
sys.exit(7)
"""

        class LocalCommand(_BlockedSshCommand):
            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                return managed_popen(
                    [sys.executable, "-c", helper_code],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

        backend = _opened_session(
            RemoteSessionRequest("local", LocalCommand(), TEST_PROCESS),
            DeploymentResult("/helper.py", "digest", False),
        )
        try:
            backend._start_event_drain()
            backend.helper_process.wait(timeout=5)
            with pytest.raises(SessionError):
                backend.wait_for_openocd_exit(5)
            assert backend._openocd_returncode == 0
        finally:
            with suppress(BaseException):
                backend.close()

    def test_backend_close_waits_for_helper_child_kill_fallback(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            child_pid_path = Path(directory) / "child.pid"

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

            marker = "ZRO_READY_ignore_term"
            child_code = (
                "from pathlib import Path;"
                "import os,signal,sys,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                "Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii');"
                "print(sys.argv[1], flush=True);"
                "time.sleep(30)"
            )
            remote_process = RemoteProcess(
                (sys.executable, "-c", child_code, marker, str(child_pid_path)),
                readiness_marker=marker,
            )
            backend = _opened_session(
                RemoteSessionRequest("local", LocalCommand(), process=remote_process),
                DeploymentResult(str(helper), "digest", False),
            )
            workspace = Path(backend.allocation.remote_workspace)
            child_pid = None
            child_pidfd = None
            try:
                backend._start_process(())
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                child_pidfd = os.pidfd_open(child_pid)

                backend.close()

                assert backend.closed
                assert backend.helper_process.returncode == 0
                assert not workspace.exists()
                _assert_pidfd_exited(child_pidfd)
            finally:
                with suppress(BaseException):
                    backend.close()
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


def _opened_session(*args, **kwargs):
    session = RemoteSession(*args, **kwargs)
    session._open_helper()
    return session
