# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.remote import rtt as rtt_module
from zephyr_remote_openocd.remote.backend import SshHelperBackend, SshHelperSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
    StagedFile,
)
from zephyr_remote_openocd.remote.paths import ADDRESS_TOKEN, PathPlanner
from zephyr_remote_openocd.remote.protocol import (
    encode_message,
)
from zephyr_remote_openocd.remote.rtt import RttClientError, run_rtt_client
from zephyr_remote_openocd.remote.services import (
    LOOPBACK_RANGE,
)
from zephyr_remote_openocd.remote.session import (
    SessionError,
)
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess
from zephyr_remote_openocd.remote.staging import build_archive

from tests.process_support import read_line, read_lines
from tests.support import ROOT


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

        def stderr_tail(self, *, wait=False):
            return b""

        def close_stderr(self):
            self.stderr.close()

    class Command:
        def __init__(self, process):
            self.process = process
            self.calls = []

        def popen(self, host, remote_command, *extra_args):
            self.calls.append((host, remote_command, extra_args))
            return self.process

    @staticmethod
    def session(command):
        session = object.__new__(SshHelperSession)
        session.request = RemoteSessionRequest("target", command)
        session.forward_start_timeout = 1
        session.forwards = []
        session.closed = False
        session.output_handler = None
        session.process_returncode = None
        session.reader_error = None
        session.reader_thread = None
        session._terminal_reason = None
        session._state_lock = threading.RLock()
        return session

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
            patch.object(SshHelperSession, "_await_forward_ready", return_value=True),
            patch("zephyr_remote_openocd.remote.backend.socket.create_connection") as connect,
        ):
            session._start_forwards((service,), "127.64.1.1")
        connect.assert_not_called()
        assert command.calls[0][1].startswith("python3 -c ")
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
        with patch.object(SshHelperSession, "_read_event", side_effect=events):
            SshHelperSession(
                RemoteSessionRequest("target", command),
                DeploymentResult("/helper.py", "digest", False),
                1,
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
            patch.object(SshHelperSession, "_read_event", side_effect=startup_error),
            patch.object(
                SshHelperSession,
                "_stop_process",
                side_effect=RuntimeError("process cleanup failed"),
            ),
            pytest.raises(SessionError, match="invalid helper response") as raised,
        ):
            SshHelperSession(
                RemoteSessionRequest("target", command),
                DeploymentResult("/helper.py", "digest", False),
                1,
            )

        assert raised.value is startup_error
        assert any("process cleanup failed" in note for note in raised.value.__notes__)

    def test_session_start_error_survives_forward_cleanup_failure(self):
        helper = self.Process()
        helper.stdin = io.BytesIO()
        command = self.Command(helper)
        session = self.session(command)
        session.request = RemoteSessionRequest(
            "target", command, process=RemoteProcess(("openocd",))
        )
        session.helper_process = helper
        session.allocation = SessionAllocation("session", "/workspace")
        service = Service("gdb", 1234, 3333)
        startup_error = SessionError("forward startup failed")

        with (
            patch.object(session, "_await_process_ready", return_value="127.64.1.1"),
            patch.object(session, "_start_forwards", side_effect=startup_error),
            patch.object(
                session,
                "_close_forwards",
                side_effect=RuntimeError("forward cleanup failed"),
            ),
            pytest.raises(SessionError, match="forward startup failed") as raised,
        ):
            session.start((service,))

        assert raised.value is startup_error
        assert any("forward cleanup failed" in note for note in raised.value.__notes__)

    def test_dynamic_forward_error_survives_rollback_failure(self):
        existing = self.Process()
        added = self.Process()
        session = self.session(self.Command(self.Process()))
        session.descriptor = SessionDescriptor(
            SessionAllocation("session", "/workspace"), "127.64.1.1"
        )
        session.forwards = [existing]
        forward_error = SessionError("forward failed")

        def fail_after_starting_forward(*_args):
            session.forwards.append(added)
            raise forward_error

        with (
            patch.object(session, "_start_forwards", side_effect=fail_after_starting_forward),
            patch.object(
                session,
                "_stop_process",
                side_effect=RuntimeError("forward rollback failed"),
            ),
            pytest.raises(SessionError, match="forward failed") as raised,
        ):
            session.forward((Service("rtt", 19021, 19021),))

        assert raised.value is forward_error
        assert any("forward rollback failed" in note for note in raised.value.__notes__)
        assert session.forwards == [existing]

    def test_wait_error_survives_session_cleanup_failure(self):
        session = self.session(self.Command(self.Process()))
        wait_error = SessionError("event stream failed")

        with (
            patch.object(session, "poll", side_effect=wait_error),
            patch.object(
                session,
                "close",
                side_effect=RuntimeError("session cleanup failed"),
            ),
            pytest.raises(SessionError, match="event stream failed") as raised,
        ):
            session.wait()

        assert raised.value is wait_error
        assert any("session cleanup failed" in note for note in raised.value.__notes__)

    def test_stale_gdb_forward_cannot_mask_current_forward_failure(self):
        stale = self.Process()
        read_fd, write_fd = os.pipe()
        stale.stdout = os.fdopen(read_fd, "rb")
        command = self.Command(stale)
        session = self.session(command)
        service = Service("gdb", self.port(), 3333)
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", service.local_port))
            listener.listen()
        except OSError:
            listener.close()
            stale.stdout.close()
            os.close(write_fd)
            pytest.skip("sandbox cannot create stale listener")
        with (
            patch("zephyr_remote_openocd.remote.backend.socket.create_connection") as connect,
            pytest.raises(SessionError, match="did not become ready"),
        ):
            try:
                session._start_forwards((service,), "127.64.1.1")
            finally:
                listener.close()
                os.close(write_fd)
        connect.assert_not_called()
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
        class FailingProcess(self.Process):
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
        helper = self.Process()
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

        class FailingHelper(self.Process):
            def __init__(self):
                super().__init__()
                self.fail_termination = True

            def terminate(self):
                self.terminate_calls += 1
                if self.fail_termination:
                    raise RuntimeError("forced stop failed")
                self.returncode = 0

        helper = FailingHelper()
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
        class StuckHelper(self.Process):
            def __init__(self):
                super().__init__()
                self.stdin = io.BytesIO()
                self.wait_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("helper", timeout)
                return self.returncode

        helper = StuckHelper()
        session = self.session(self.Command(helper))
        session.helper_process = helper

        with pytest.raises(subprocess.TimeoutExpired):
            session.close()

        assert helper.terminate_calls == 1
        assert session.closed

    @pytest.mark.parametrize("returncode", (0, 7))
    def test_poll_reports_consumed_status_while_helper_remains_alive(self, returncode):
        session = self.session(self.Command(self.Process()))
        session.helper_process = session.request.ssh_command.process
        session.process_returncode = None
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
        assert event_consumed.wait(2)
        try:
            assert session.reader_thread.is_alive()
            assert session.helper_process.poll() is None
            assert session.poll() == returncode
        finally:
            release_reader.set()
            session.reader_thread.join(2)

    def test_poll_preserves_reader_error_before_known_process_exit(self):
        session = self.session(self.Command(self.Process()))
        session.helper_process = session.request.ssh_command.process
        session.process_returncode = 7
        session.reader_error = RuntimeError("protocol failed")

        with pytest.raises(SessionError, match="helper event stream failed: protocol failed"):
            session.poll()

    def test_poll_preserves_helper_exit_before_process_exit_event(self):
        session = self.session(self.Command(self.Process(returncode=9)))
        session.helper_process = session.request.ssh_command.process
        session.process_returncode = None
        session.reader_error = None
        session.reader_thread = None

        assert session.poll() == 9


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
        polls = []

        def poll_session():
            polls.append(None)
            return 0 if len(polls) == 2 else None

        with (
            os.fdopen(input_read, "rb", buffering=0) as stdin,
            tempfile.TemporaryFile("w+b") as stdout,
            patch.object(rtt_module, "_connect", return_value=(connection, b"")),
            patch.object(
                rtt_module.select,
                "select",
                return_value=([stdin.fileno()], [], []),
            ) as select_call,
        ):
            input_fd = stdin.fileno()
            assert run_rtt_client(5555, poll_session, stdin=stdin, stdout=stdout) == 0

        assert polls == [None, None]
        select_call.assert_called_once_with((input_fd, connection), (), (), 0.1)

    def test_full_input_queue_pauses_and_resumes_stdin_after_partial_send(self, monkeypatch):
        class Connection:
            def __init__(self):
                self.sent = []

            def send(self, payload):
                self.sent.append(bytes(payload))
                return 2

            def close(self):
                pass

        connection = Connection()
        monkeypatch.setattr(rtt_module, "_INPUT_CHUNK_SIZE", 4)
        monkeypatch.setattr(rtt_module, "_MAX_PENDING_INPUT", 4)
        input_read, input_write = os.pipe()
        os.write(input_write, b"abcdefgh")
        os.close(input_write)
        polls = []

        def poll_session():
            polls.append(None)
            return 0 if len(polls) == 4 else None

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
                    side_effect=(
                        ([input_fd], [], []),
                        ([], [connection], []),
                        ([input_fd], [connection], []),
                    ),
                ) as select_call,
            ):
                assert run_rtt_client(5555, poll_session, stdin=stdin, stdout=stdout) == 0

        assert polls == [None, None, None, None]
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
                session.helper_process.returncode = 0
                return b""

            def close(self):
                pass

        class Reader:
            def __init__(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def join(self):
                session.process_returncode = 0
                self.alive = False

        session = TestForwardingLifecycle.session(
            TestForwardingLifecycle.Command(TestForwardingLifecycle.Process())
        )
        session.helper_process = session.request.ssh_command.process
        session.process_returncode = None
        session.reader_error = None
        session.reader_thread = Reader()
        connection = Connection()
        with (
            tempfile.TemporaryFile("w+b") as stream,
            patch.object(rtt_module, "_connect", return_value=(connection, b"connected")),
            patch.object(rtt_module.select, "select", return_value=([connection], [], [])),
        ):
            assert run_rtt_client(5555, session.poll, stdin=stream, stdout=stream) == 0

    def test_immediate_forwarded_channel_failure_is_authoritative(self):
        def server(listener):
            with listener, listener.accept()[0]:
                pass

        port, thread = self._listener(server)
        with pytest.raises(RttClientError, match="remote channel"):
            run_rtt_client(port, lambda: None, startup_timeout=1)
        thread.join(2)

    def test_tty_preserves_signals_and_restores_complete_state(self):
        class Connection:
            def recv(self, _size):
                return b""

            def close(self):
                pass

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
        poll_results = iter((None, None, 0))
        with (
            tempfile.TemporaryFile("w+b") as stream,
            patch.object(rtt_module, "_connect", return_value=(connection, b"")),
            patch.object(rtt_module.os, "isatty", return_value=True),
            patch.object(rtt_module.os, "read", return_value=b""),
            patch.object(
                rtt_module.select,
                "select",
                side_effect=[([stream.fileno()], [], []), ([connection], [], [])],
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
                    lambda: next(poll_results),
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
        archive = build_archive((StagedFile(source, "firmware.bin"),))

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
        class LocalCommand:
            def run_stream(self, host, command, stream, timeout=60):
                stream.read()
                return subprocess.CompletedProcess(command, 0, response, b"")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "firmware.bin"
            source.write_bytes(b"firmware")
            session = object.__new__(SshHelperSession)
            session.request = RemoteSessionRequest("local", LocalCommand())
            session.deployment = DeploymentResult("/helper.py", "0" * 64, False)
            session.allocation = SessionAllocation("session", "/workspace")
            with pytest.raises(SessionError, match="invalid remote staging response"):
                session.stage((StagedFile(source, "firmware.bin"),))

    def test_backend_wraps_invalid_utf8_version_response(self, monkeypatch):
        class LocalCommand:
            def run(self, host, command, timeout=30):
                return subprocess.CompletedProcess(command, 0, b"\xff", b"")

        monkeypatch.setattr(
            "zephyr_remote_openocd.remote.backend.deploy_helper",
            lambda _command, _host: DeploymentResult("/helper.py", "0" * 64, False),
        )

        with pytest.raises(SessionError, match="invalid remote OpenOCD version response"):
            SshHelperBackend().openocd_version(LocalCommand(), "local", ("openocd",))

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
                events = []
                while not any(event["type"] == "PROCESS_READY" for event in events):
                    events.append(json.loads(read_line(process.stdout)))
                ready = next(event for event in events if event["type"] == "PROCESS_READY")
                child_pid = ready["child_pid"]
                assert any(
                    event["type"] == "CHILD_OUTPUT" and event["payload"] == marker
                    for event in events
                )
                process.stdin.write(encode_message("STOP"))
                process.stdin.flush()
                assert process.wait(timeout=8) == 0
                assert not workspace.exists()
                with pytest.raises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

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
                events = []
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
                process.terminate()
                assert process.wait(timeout=8) == 0
                assert not workspace.exists()
                with pytest.raises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

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
                process.stdin.close()
                assert process.wait(timeout=8) == 0
                assert not workspace.exists()
                with pytest.raises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

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

            class LocalCommand:
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
            backend = SshHelperSession(
                request,
                DeploymentResult(str(helper), "digest", False),
                0.1,
                lambda stream, payload, line_end: output.append((stream, payload, line_end)),
            )
            try:
                backend.stage(())
                descriptor = backend.start(())
                assert ipaddress.ip_address(descriptor.remote_address) in LOOPBACK_RANGE
                assert backend.wait(5) == 6
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
            (None, 7, "did not produce SESSION_CLOSED"),
        ),
        ids=(
            "requested-success",
            "requested-then-error",
            "requested-then-malformed",
            "error-and-nonzero",
            "requested-terminal-nonzero",
            "malformed-terminal",
            "nonzero-without-terminal",
        ),
    )
    def test_backend_close_validates_requested_helper_shutdown(self, terminal, exit_code, expected):
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

        class LocalCommand:
            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument,unused-argument
                return managed_popen(
                    [sys.executable, "-c", helper_code],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

        backend = SshHelperSession(
            RemoteSessionRequest("local", LocalCommand()),
            DeploymentResult("/helper.py", "digest", False),
            0.1,
        )
        backend._start_event_drain()
        try:
            if expected is None:
                backend.close()
                assert backend.closed
                assert backend._terminal_reason == "requested"
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

        class LocalCommand:
            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument,unused-argument
                return managed_popen(
                    [sys.executable, "-c", helper_code],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

        backend = SshHelperSession(
            RemoteSessionRequest("local", LocalCommand()),
            DeploymentResult("/helper.py", "digest", False),
            0.1,
        )
        backend._start_event_drain()
        assert backend.reader_thread is not None
        deadline = time.monotonic() + 2
        while backend._terminal_reason is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend._terminal_reason == "requested"
        try:
            with pytest.raises(SessionError, match="before STOP"):
                backend.close()
            assert backend.closed
        finally:
            with suppress(BaseException):
                backend.close()

    def test_backend_close_keeps_helper_failure_primary_over_forward_cleanup(self):
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

        class LocalCommand:
            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument,unused-argument
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
                self.stdin = None
                self.stdout = None
                self.stderr = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1
                raise RuntimeError("forward cleanup failed")

            def kill(self):
                raise RuntimeError("forward cleanup failed")

            def wait(self, timeout=None):  # pylint: disable=unused-argument
                return self.returncode

        backend = SshHelperSession(
            RemoteSessionRequest("local", LocalCommand()),
            DeploymentResult("/helper.py", "digest", False),
            0.1,
        )
        forward = FailingForward()
        backend.forwards = [forward]
        backend._start_event_drain()
        try:
            with pytest.raises(SessionError, match="cleanup failed") as raised:
                backend.close()
            assert any("forward cleanup failed" in note for note in raised.value.__notes__)
            assert backend.forwards == []
            assert backend.closed

            forward.terminate = lambda: setattr(forward, "returncode", 0)
            backend.close()
            assert backend.forwards == []
            assert backend.closed
        finally:
            with suppress(BaseException):
                backend.close()

    def test_backend_does_not_mask_helper_failure_after_close_event(self):
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

        class LocalCommand:
            def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument,unused-argument
                return managed_popen(
                    [sys.executable, "-c", helper_code],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

        backend = SshHelperSession(
            RemoteSessionRequest("local", LocalCommand()),
            DeploymentResult("/helper.py", "digest", False),
            0.1,
        )
        try:
            backend._start_event_drain()
            backend.helper_process.wait(timeout=5)
            assert backend.wait(5) == 7
        finally:
            backend.close()

    def test_backend_close_waits_for_helper_child_kill_fallback(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            child_pid_path = Path(directory) / "child.pid"

            class LocalCommand:
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
            backend = SshHelperSession(
                RemoteSessionRequest("local", LocalCommand(), process=remote_process),
                DeploymentResult(str(helper), "digest", False),
                0.1,
            )
            workspace = Path(backend.allocation.remote_workspace)
            child_pid = None
            try:
                backend.start(())
                child_pid = int(child_pid_path.read_text(encoding="ascii"))

                backend.close()

                assert backend.closed
                assert backend.helper_process.returncode == 0
                assert not workspace.exists()
                with pytest.raises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                with suppress(BaseException):
                    backend.close()
                if child_pid is not None:
                    with suppress(ProcessLookupError):
                        os.kill(child_pid, signal.SIGKILL)

    def test_backend_reader_failure_terminates_session(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand:
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

            def fail_on_output(stream, payload, line_end):
                raise RuntimeError("output sink failed")

            backend = SshHelperSession(
                request,
                DeploymentResult(str(helper), "digest", False),
                0.1,
                fail_on_output,
            )
            workspace = backend.allocation.remote_workspace
            try:
                backend.stage(())
                backend.start(())
                with pytest.raises(SessionError, match="helper event stream failed"):
                    backend.wait(5)
                assert backend.closed
                assert not Path(workspace).exists()
            finally:
                with suppress(BaseException):
                    backend.close()
