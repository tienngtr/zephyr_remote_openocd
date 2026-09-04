# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
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

from tests.support import ROOT


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
            patch.object(SshHelperSession, "_listener_ready", return_value=True),
            patch("zephyr_remote_openocd.remote.backend.socket.create_connection") as connect,
        ):
            session._start_forwards((service,), "127.64.1.1")
        connect.assert_not_called()
        session._close_forwards()
        assert process.terminate_calls == 1

    def test_stale_gdb_forward_cannot_mask_current_forward_failure(self):
        class RaceProcess(self.Process):
            def __init__(self):
                super().__init__()
                self.poll_count = 0

            def poll(self):
                self.poll_count += 1
                return None if self.poll_count == 1 else 255

        stale = RaceProcess()
        command = self.Command(stale)
        session = self.session(command)
        service = Service("gdb", self.port(), 3333)
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", service.local_port))
            listener.listen()
        except OSError:
            listener.close()
            pytest.skip("sandbox cannot create stale listener")
        with (
            patch("zephyr_remote_openocd.remote.backend.socket.create_connection") as connect,
            pytest.raises(SessionError, match="SSH forwarding failed"),
        ):
            try:
                session._start_forwards((service,), "127.64.1.1")
            finally:
                listener.close()
        connect.assert_not_called()
        session._close_forwards()
        assert stale.terminate_calls == 0

    def test_forward_cleanup_is_reverse_order_and_idempotent(self):
        first = self.Process()
        second = self.Process()
        session = self.session(self.Command(first))
        session.forwards = [first, second]
        session._close_forwards()
        session._close_forwards()
        assert second.terminate_calls == 1
        assert first.terminate_calls == 1


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
                connection.sendall(b"remote-output")
                received.append(connection.recv(64))

        port, thread = self._listener(server)
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        os.write(input_write, b"local-input")
        os.close(input_write)
        with (
            os.fdopen(input_read, "rb", buffering=0) as stdin,
            os.fdopen(output_write, "wb", buffering=0) as stdout,
        ):
            assert run_rtt_client(port, lambda: None, stdin=stdin, stdout=stdout) is None
        thread.join(2)
        assert received == [b"local-input"]
        assert os.read(output_read, 64) == b"remote-output"
        os.close(output_read)

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
            assert run_rtt_client(5555, lambda: None, stdin=stream, stdout=stream) is None
        configured = set_attributes.call_args_list[0].args[2]
        assert not configured[3] & rtt_module.termios.ICANON
        assert not configured[3] & rtt_module.termios.ECHO
        assert configured[3] & rtt_module.termios.ISIG
        assert set_attributes.call_args_list[-1].args[2] == original


class TestRealProcessHelper:
    def test_helper_applies_requested_environment_before_child_executes(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
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
                process.stdout.readline()
                process.stdout.readline()
                process.stdin.write(
                    encode_message(
                        "START_OPENOCD",
                        argv=[
                            sys.executable,
                            "-c",
                            "import os; print(os.environ['ZRO_TEST_FORWARD'])",
                        ],
                        environment={"ZRO_TEST_FORWARD": "before-config"},
                    )
                )
                process.stdin.flush()
                events = [json.loads(line) for line in process.stdout]
                output = next(event for event in events if event["type"] == "CHILD_OUTPUT")
                assert output == {
                    "version": 1,
                    "type": "CHILD_OUTPUT",
                    "stream": "stdout",
                    "payload": "before-config",
                }
                assert process.wait(timeout=5) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_helper_forwards_environment_to_openocd_configuration(self):
        executable = shutil.which("openocd")
        if executable is None:
            pytest.skip("openocd is not installed")
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            config = Path(directory) / "environment.cfg"
            config.write_text(
                "set zro_forwarded_value $::env(ZRO_CONFIG_VALUE)\n"
                "echo ZRO_CONFIG_VALUE=$zro_forwarded_value\n"
                "shutdown\n"
            )
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
                process.stdout.readline()
                process.stdout.readline()
                process.stdin.write(
                    encode_message(
                        "START_OPENOCD",
                        argv=[executable, "-f", str(config)],
                        environment={"ZRO_CONFIG_VALUE": "channel-1"},
                    )
                )
                process.stdin.flush()
                events = [json.loads(line) for line in process.stdout]
                output = next(
                    event
                    for event in events
                    if event["type"] == "CHILD_OUTPUT"
                    and event["payload"] == "ZRO_CONFIG_VALUE=channel-1"
                )
                assert output["stream"] in ("stdout", "stderr")
                assert process.wait(timeout=15) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_controller_rejects_malformed_and_unsupported_version(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        for frame in (b"not-json\n", b'{"version":2,"type":"STOP"}\n'):
            with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
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
                    process.stdout.readline()
                    process.stdout.readline()
                    process.stdin.write(frame)
                    process.stdin.flush()
                    error = json.loads(process.stdout.readline())
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
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
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
                assert json.loads(process.stdout.readline())["type"] == "HELLO"
                json.loads(process.stdout.readline())
                marker = "ZRO_READY_unit"
                child_code = (
                    "import socket,sys,time;"
                    "s=socket.socket();s.bind((sys.argv[1],int(sys.argv[2])));s.listen();"
                    "print(sys.argv[3],flush=True);time.sleep(30)"
                )
                process.stdin.write(
                    encode_message(
                        "START_OPENOCD",
                        argv=[
                            sys.executable,
                            "-c",
                            child_code,
                            ADDRESS_TOKEN,
                            str(remote_port),
                            marker,
                        ],
                        environment={},
                        required_paths=[],
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        readiness_marker=marker,
                        readiness_timeout=5,
                    )
                )
                process.stdin.flush()
                events = []
                while not any(event["type"] == "SERVICE_READY" for event in events):
                    events.append(json.loads(process.stdout.readline()))
                assert events[0]["type"] == "PROCESS_STARTED"
                assert any(
                    event["type"] == "CHILD_OUTPUT" and event["payload"] == marker
                    for event in events
                )
                process.stdin.write(encode_message("STOP"))
                process.stdin.flush()
                assert process.wait(timeout=8) == 0
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

    def test_output_exit_status_and_workspace_cleanup(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
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
                assert json.loads(process.stdout.readline())["type"] == "HELLO"
                created = json.loads(process.stdout.readline())
                command = [
                    sys.executable,
                    "-c",
                    'import sys;print("out");print("err",file=sys.stderr);sys.exit(7)',
                ]
                process.stdin.write(
                    encode_message("START_OPENOCD", argv=command, environment={}, required_paths=[])
                )
                process.stdin.flush()
                events = [json.loads(line) for line in process.stdout]
                assert process.wait(timeout=5) == 0
                assert events[0]["type"] == "PROCESS_STARTED"
                outputs = {
                    (event["stream"], event["payload"])
                    for event in events
                    if event["type"] == "CHILD_OUTPUT"
                }
                assert outputs == {("stdout", "out"), ("stderr", "err")}
                exit_event = next(event for event in events if event["type"] == "PROCESS_EXIT")
                assert exit_event["returncode"] == 7
                assert not Path(created["remote_workspace"]).exists()
                assert process.stderr.read() == b""
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_controller_signal_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
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
                assert json.loads(process.stdout.readline())["type"] == "HELLO"
                created = json.loads(process.stdout.readline())
                workspace = Path(created["remote_workspace"])
                command = [sys.executable, "-c", "import time; time.sleep(30)"]
                process.stdin.write(
                    encode_message("START_OPENOCD", argv=command, environment={}, required_paths=[])
                )
                process.stdin.flush()
                started = json.loads(process.stdout.readline())
                assert started["type"] == "PROCESS_STARTED"
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

    def test_controller_eof_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
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
                process.stdout.readline()
                created = json.loads(process.stdout.readline())
                workspace = Path(created["remote_workspace"])
                process.stdin.write(
                    encode_message(
                        "START_OPENOCD",
                        argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                    )
                )
                process.stdin.flush()
                started = json.loads(process.stdout.readline())
                assert started["type"] == "PROCESS_STARTED"
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
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
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
                assert json.loads(process.stdout.readline())["type"] == "HELLO"
                created = json.loads(process.stdout.readline())
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
                    encode_message(
                        "START_OPENOCD",
                        argv=command,
                        environment={},
                        required_paths=[],
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        readiness_marker=marker,
                        readiness_timeout=0.5,
                    )
                )
                process.stdin.flush()
                events = [json.loads(line) for line in process.stdout]
                assert events[0]["type"] == "PROCESS_STARTED"
                assert any(event["type"] == "CHILD_OUTPUT" for event in events)
                assert process.wait(timeout=8) == 0
                assert not workspace.exists()
                assert events[-1]["type"] == "ERROR"
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_backend_returns_child_status_and_relays_output(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand:
                argv_prefix = ("local-test",)

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
                "openocd",
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
