# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.remote import rtt as rtt_module
from zephyr_remote_openocd.remote.backend import SshHelperSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
    StagedFile,
)
from zephyr_remote_openocd.remote.paths import ADDRESS_TOKEN
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

from tests.process_support import read_line, read_lines
from tests.support import ROOT


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
        session.request = RemoteSessionRequest("dot4", command)
        session.forward_start_timeout = 1
        session.forwards = []
        session.closed = False
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
        session.request = RemoteSessionRequest("dot4", command, process=RemoteProcess(("openocd",)))
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
        assert session.forwards == [existing, added]

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
        assert session.forwards == [failed]
        assert not session.closed

        failed.fail_termination = False
        session.close()
        assert failed.terminate_calls == 2
        assert session.forwards == []
        assert session.closed

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
        assert not session.closed

        helper.stdin.fail = False
        helper.fail_termination = False
        session.close()
        assert session.closed

    def test_poll_returns_consumed_status_while_reader_remains_alive(self):
        session = self.session(self.Command(self.Process(returncode=0)))
        session.helper_process = session.request.ssh_command.process
        session.process_returncode = None
        session.reader_error = None
        event_consumed = threading.Event()
        release_reader = threading.Event()

        def consume_session_closed():
            session.process_returncode = 0
            event_consumed.set()
            release_reader.wait()

        session.reader_thread = threading.Thread(target=consume_session_closed)
        session.reader_thread.start()
        assert event_consumed.wait(2)
        try:
            assert session.reader_thread.is_alive()
            assert session.poll() == 0
        finally:
            release_reader.set()
            session.reader_thread.join(2)


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
    @pytest.mark.parametrize(
        "members",
        (
            (("../escape", tarfile.REGTYPE, None),),
            (("nested/link", tarfile.SYMTYPE, "target"),),
            (("fifo", tarfile.FIFOTYPE, None),),
            (
                ("duplicate", tarfile.REGTYPE, None),
                ("duplicate", tarfile.REGTYPE, None),
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

    def test_backend_rejects_mismatched_staging_confirmation(self):
        class LocalCommand:
            def run_stream(self, host, command, stream, timeout=60):
                stream.read()
                return subprocess.CompletedProcess(
                    command,
                    0,
                    encode_message(
                        "STAGED",
                        byte_count=999,
                        sha256="0" * 64,
                        files=["firmware.bin"],
                    ),
                    b"",
                )

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "firmware.bin"
            source.write_bytes(b"firmware")
            session = object.__new__(SshHelperSession)
            session.request = RemoteSessionRequest("local", LocalCommand())
            session.deployment = DeploymentResult("/helper.py", "0" * 64, False)
            session.allocation = SessionAllocation("session", "/workspace")
            with pytest.raises(SessionError, match="invalid remote staging response"):
                session.stage((StagedFile(source, "firmware.bin"),))

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
                    if event["type"] == "CHILD_OUTPUT"
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
                    return subprocess.Popen(
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
                lambda stream, payload: output.append((stream, payload)),
            )
            try:
                backend.stage(())
                descriptor = backend.start(())
                assert ipaddress.ip_address(descriptor.remote_address) in LOOPBACK_RANGE
                assert backend.wait(5) == 6
                assert output == [("stdout", "hello")]
            finally:
                backend.close()

    def test_backend_reader_failure_terminates_session(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand:
                def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                    return subprocess.Popen(
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

            def fail_on_output(stream, payload):
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
                backend.close()
