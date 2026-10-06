# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import hashlib
import ipaddress
import shlex
import shutil
import socket
import tempfile
import time
from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.remote import (
    RemoteProcess,
    RemoteSession,
    RemoteSessionRequest,
    Service,
    SessionError,
    StagedFile,
)
from zephyr_remote_openocd.remote.arguments import ArgumentTemplate, SessionValue
from zephyr_remote_openocd.remote.deploy import deploy_helper
from zephyr_remote_openocd.remote.forwarding import _ForwardManager
from zephyr_remote_openocd.remote.ssh import SshCommand, SshLocalForward

from tests.process_support import read_line

pytestmark = pytest.mark.ssh
REMOTE_FAILURE_EXIT_CODE = 7

REMOTE_ECHO = b"""\
import select, socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('127.0.0.1', 0))
s.listen()
print(s.getsockname()[1], flush=True)
while True:
    ready = select.select([sys.stdin.buffer, s], [], [], 0.2)[0]
    if sys.stdin.buffer in ready and not sys.stdin.buffer.read(1):
        break
    if s in ready:
        c, _ = s.accept()
        with c:
            data = c.recv(4096)
            c.sendall(data)
"""

REMOTE_SESSION_ECHO = """
import socket
import sys

listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind((sys.argv[1], int(sys.argv[2])))
listener.listen()
print("ZRO_TEST_READY", flush=True)
while True:
    connection, _ = listener.accept()
    with connection:
        while True:
            data = connection.recv(65536)
            if not data:
                break
            connection.sendall(data)
"""


def session_echo_process() -> RemoteProcess:
    return RemoteProcess(
        ("python3", "-c", REMOTE_SESSION_ECHO, "{address}", "3333"),
        required_output_sentinels=("ZRO_TEST_READY",),
        literal_prefix=3,
        argv_templates=((3, ArgumentTemplate((SessionValue.ADDRESS,))),),
    )


def assert_remote_marker(ssh: SshCommand, host: str):
    result = ssh.run(host, "printf zro_ssh_marker", timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b"zro_ssh_marker"


def free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_echo(port: int, payload: bytes, timeout: float):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1) as connection:
                connection.sendall(payload)
                if connection.recv(len(payload)) != payload:
                    raise AssertionError("forwarded echo payload differed")
                return
        except OSError as error:
            last_error = error
            time.sleep(0.1)
    raise AssertionError(f"forwarded endpoint was not ready: {last_error}")


def stop_and_close(process, timeout: float = 20):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=timeout)
    process.close_stderr()
    for stream in (process.stdin, process.stdout):
        if stream is not None and not stream.closed:
            stream.close()


class TestConfiguredSshIntegration:
    @pytest.fixture(autouse=True)
    def inventory_setup(self, ssh_host, ssh_settings):
        self.host = ssh_host
        self.ssh = SshCommand(ssh_settings.ssh_command)

    def test_configured_ssh_and_fixed_arguments(self):
        assert_remote_marker(self.ssh, self.host)
        assert_remote_marker(
            SshCommand((*self.ssh.argv_prefix, "-o", "ConnectTimeout=10")), self.host
        )

    def test_explicit_path_with_nonstandard_executable_name(self, tmp_path):
        configured = self.ssh.argv_prefix[0]
        source = shutil.which(configured) if "/" not in configured else configured
        assert source is not None, f"configured SSH executable not found: {configured}"
        alternate = tmp_path / "custom-ssh"
        alternate.symlink_to(Path(source).resolve())
        command = SshCommand((str(alternate), *self.ssh.argv_prefix[1:]))

        assert_remote_marker(command, self.host)


class TestSshTransportIntegration:
    @pytest.fixture(autouse=True)
    def inventory_setup(self, ssh_host, ssh_settings):
        self.host = ssh_host
        self.ssh_settings = ssh_settings
        self.ssh = SshCommand(ssh_settings.ssh_command)

    @pytest.mark.parametrize("occupied", (False, True), ids=("available", "occupied"))
    def test_forwarding_conflicts_cannot_report_an_unbound_forward_ready(self, occupied):
        conflicts = ("-o", "ExitOnForwardFailure=no", "-o", "ClearAllForwardings=yes")
        ssh = SshCommand((*self.ssh.argv_prefix, *conflicts))
        manager = _ForwardManager(ssh, self.host)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            if occupied:
                listener.listen()
            else:
                listener.close()
            service = Service("gdb", port, 3333)
            try:
                if occupied:
                    with pytest.raises(SessionError):
                        manager.start((service,), "127.0.0.1")
                    assert not manager.has_forwards
                else:
                    manager.start((service,), "127.0.0.1")
                    assert manager.services == (service,)
                    # Verify a listener exists immediately after actual readiness.
                    with socket.create_connection(("127.0.0.1", port), timeout=20):
                        pass
            finally:
                manager.close()
            assert not manager.has_forwards
            if not occupied:
                with socket.socket() as after_cleanup:
                    after_cleanup.bind(("127.0.0.1", port))

    def test_forwarding_and_session_lifecycle_use_configured_client(self):
        encoded = base64.b64encode(REMOTE_ECHO).decode("ascii")
        command = f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\""
        helper_process = self.ssh.popen(self.host, command)
        tunnel = None
        try:
            assert helper_process.stdout is not None
            line = read_line(helper_process.stdout)
            if not line:
                pytest.fail(helper_process.stderr_tail().decode(errors="replace"))
            remote_port = int(line)
            local_port = free_loopback_port()
            sentinel = "ZRO_FORWARD_READY"
            tunnel = self.ssh.popen(
                self.host,
                _ForwardManager._ready_command(sentinel),
                local_forward=SshLocalForward(local_port, "127.0.0.1", remote_port),
            )
            assert _ForwardManager._await_ready(tunnel, sentinel, time.monotonic() + 20)
            wait_for_echo(local_port, b"zro_forwarding", 20)

            assert helper_process.stdin is not None
            helper_process.stdin.close()
            helper_process.wait(timeout=20)
            with pytest.raises(AssertionError):
                wait_for_echo(local_port, b"must_not_echo", 1)

            assert tunnel.stdin is not None
            tunnel.stdin.close()
            assert tunnel.wait(timeout=20) == 0
        finally:
            stop_and_close(helper_process)
            stop_and_close(tunnel)

    def test_streaming_preserves_content_and_reports_remote_failure(self):
        payloads = (
            b"",
            b"small textual input\n",
            bytes(range(256)) * 4,
            bytes(range(256)) * 4096,
        )
        command = (
            "python3 -c 'import hashlib,sys; d=sys.stdin.buffer.read(); "
            "print(len(d), hashlib.sha256(d).hexdigest())'"
        )
        for payload in payloads:
            result = self.ssh.run(self.host, command, input_data=payload, timeout=30)
            expected = f"{len(payload)} {hashlib.sha256(payload).hexdigest()}\n".encode()
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            assert result.stdout == expected

        failed = self.ssh.run(
            self.host,
            "python3 -c 'import sys; sys.stdin.buffer.read(); "
            f"raise SystemExit({REMOTE_FAILURE_EXIT_CODE})'",
            input_data=b"stream before remote failure",
            timeout=20,
        )
        assert failed.returncode == REMOTE_FAILURE_EXIT_CODE

    def test_protocol_v1_helper_vertical_slice(self):
        """Exercise the production transport path with an explicit test process."""
        first = deploy_helper(self.ssh, self.host)
        second = deploy_helper(self.ssh, self.host)
        assert first.path == second.path
        assert second.reused
        local_port = free_loopback_port()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "payload.bin"
            source.write_bytes(bytes(range(256)) + b"\0remote")
            request = RemoteSessionRequest(
                self.host,
                self.ssh,
                session_echo_process(),
                (StagedFile(source, PurePosixPath("nested/payload.bin")),),
                (Service("gdb", local_port, 3333),),
            )
            session = RemoteSession.open(request)
            assert session.descriptor is not None
            descriptor = session.descriptor
            try:
                address = ipaddress.ip_address(descriptor.remote_address)
                assert address in ipaddress.ip_network("127.64.0.0/10")
                check = self.ssh.run(
                    self.host,
                    "python3 -c "
                    + shlex.quote(
                        "import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                        "print(oct(p.stat().st_mode&0o777), "
                        "(p/'staged/nested/payload.bin').stat().st_size)"
                    )
                    + " "
                    + shlex.quote(descriptor.remote_workspace),
                    timeout=20,
                )
                assert check.returncode == 0, check.stderr.decode(errors="replace")
                assert check.stdout.strip() == b"0o700 263"
                wait_for_echo(local_port, b"helper_round_trip", 20)
            finally:
                workspace = descriptor.remote_workspace
                session.close()
            gone = self.ssh.run(
                self.host,
                "python3 -c "
                + shlex.quote(
                    "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                    "artifacts=(p, p.with_name('.'+p.name+'.lease'), "
                    "p.with_name('.'+p.name+'.closed')); "
                    "sys.exit(any(path.exists() for path in artifacts))"
                )
                + " "
                + shlex.quote(workspace),
                timeout=20,
            )
            assert gone.returncode == 0, gone.stderr.decode(errors="replace")

    def test_concurrent_sessions_isolate_identical_remote_ports(self):
        """Regression coverage for independent-session service-port isolation."""
        first_port = free_loopback_port()
        second_port = free_loopback_port()
        while second_port == first_port:
            second_port = free_loopback_port()
        first = RemoteSession.open(
            RemoteSessionRequest(
                self.host,
                self.ssh,
                session_echo_process(),
                services=(Service("gdb", first_port, 3333),),
            )
        )
        second = RemoteSession.open(
            RemoteSessionRequest(
                self.host,
                self.ssh,
                session_echo_process(),
                services=(Service("gdb", second_port, 3333),),
            )
        )
        assert first.descriptor is not None
        first_descriptor = first.descriptor
        second_descriptor = second.descriptor
        assert second_descriptor is not None
        try:
            assert first_descriptor.remote_address != second_descriptor.remote_address
            assert first_descriptor.session_id != second_descriptor.session_id
        finally:
            second.close()
            first.close()
        for descriptor in (first_descriptor, second_descriptor):
            result = self.ssh.run(
                self.host,
                f"test ! -e {shlex.quote(descriptor.remote_workspace)}",
                timeout=20,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")

    def test_preferred_address_reuses_forward_retained_by_external_master(self, tmp_path):
        """The test owns the master; production sessions own only their slaves."""
        ssh = SshCommand(
            (
                self.ssh.argv_prefix[0],
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=no",
                "-o",
                f"ControlPath={tmp_path / 'master'}",
                *self.ssh.argv_prefix[1:],
            )
        )
        local_port = free_loopback_port()
        request = RemoteSessionRequest(
            self.host,
            ssh,
            session_echo_process(),
            services=(Service("gdb", local_port, 3333),),
        )
        master = ssh.popen(self.host, "printf 'ZRO_MASTER_READY\\n'; cat >/dev/null")
        try:
            assert master.stdout is not None
            assert read_line(master.stdout).strip() == b"ZRO_MASTER_READY", master.stderr_tail()
            first = RemoteSession.open(request)
            try:
                assert first.descriptor is not None
                address = first.descriptor.remote_address
                wait_for_echo(local_port, b"first_session", 20)
            finally:
                first.close()
            assert master.poll() is None
            with socket.create_connection(("127.0.0.1", local_port), timeout=20):
                pass
            second = RemoteSession.open(request)
            try:
                assert second.descriptor is not None
                assert second.descriptor.remote_address == address
                wait_for_echo(local_port, b"reused_session", 20)
            finally:
                second.close()
            assert master.poll() is None
        finally:
            if master.stdin is not None:
                master.stdin.close()
            stop_and_close(master)
        with socket.socket() as after_cleanup:
            after_cleanup.bind(("127.0.0.1", local_port))

    def test_helper_ssh_loss_cleans_session(self):
        """Losing the helper SSH process cleans the session."""

        class RecordingSshCommand(SshCommand):
            helper_processes: list

            def __post_init__(self):
                super().__post_init__()
                object.__setattr__(self, "helper_processes", [])

            def popen(self, host, remote_command, *, local_forward=None):
                process = super().popen(host, remote_command, local_forward=local_forward)
                if remote_command.endswith(" control"):
                    self.helper_processes.append(process)
                return process

        local_port = free_loopback_port()
        ssh = RecordingSshCommand(self.ssh_settings.ssh_command)
        session = RemoteSession.open(
            RemoteSessionRequest(
                self.host,
                ssh,
                session_echo_process(),
                services=(Service("gdb", local_port, 3333),),
            )
        )
        assert session.descriptor is not None
        descriptor = session.descriptor
        try:
            helper_process = ssh.helper_processes[0]
            helper_process.terminate()
            helper_process.wait(timeout=20)
            with pytest.raises(SessionError):
                session.wait_for_openocd_exit(timeout=20)
        finally:
            with pytest.raises(SessionError, match="SESSION_CLOSED"):
                session.close()
        assert session.closed
        result = self.ssh.run(
            self.host,
            f"test ! -e {shlex.quote(descriptor.remote_workspace)}",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

    def test_owned_forward_loss_is_reported_and_session_cleans_up(self):
        """An established session reports loss of its own SSH forward."""

        class RecordingSshCommand(SshCommand):
            forwards: list

            def __post_init__(self):
                super().__post_init__()
                object.__setattr__(self, "forwards", [])

            def popen(self, host, remote_command, *, local_forward=None):
                process = super().popen(host, remote_command, local_forward=local_forward)
                if local_forward is not None:
                    self.forwards.append(process)
                return process

        local_port = free_loopback_port()
        ssh = RecordingSshCommand(self.ssh_settings.ssh_command)
        session = RemoteSession.open(
            RemoteSessionRequest(
                self.host,
                ssh,
                session_echo_process(),
                services=(Service("gdb", local_port, 3333),),
            )
        )
        assert session.descriptor is not None
        descriptor = session.descriptor
        try:
            forward = ssh.forwards[0]
            forward.terminate()
            forward.wait(timeout=20)

            with pytest.raises(SessionError) as raised:
                session.check_openocd_exit()
            assert "gdb" in str(raised.value)
            assert f"127.0.0.1:{local_port}" in str(raised.value)

            session.close()
        finally:
            session.close()

        assert session.closed
        result = self.ssh.run(
            self.host,
            f"test ! -e {shlex.quote(descriptor.remote_workspace)}",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

    def test_remote_openocd_config_consumes_forwarded_environment(self):
        """Verify allow-listed environment reaches remote OpenOCD Tcl config."""
        openocd_command = self.ssh_settings.openocd_command
        executable = openocd_command[0]
        executable_result = self.ssh.run(
            self.host, f"test -x {shlex.quote(executable)}", timeout=20
        )
        if executable_result.returncode:
            pytest.skip(f"remote OpenOCD is not executable: {executable}")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "environment.cfg"
            config.write_text(
                "set zro_forwarded_value $::env(ZRO_CONFIG_VALUE)\n"
                "echo ZRO_CONFIG_VALUE=$zro_forwarded_value\n"
                "shutdown\n"
            )
            output = []
            process = RemoteProcess(
                (*openocd_command, "-f", "{workspace}/staged/environment.cfg"),
                (("ZRO_CONFIG_VALUE", "channel_1"),),
                argv_templates=(
                    (
                        len(openocd_command) + 1,
                        ArgumentTemplate((SessionValue.WORKSPACE, "/staged/environment.cfg")),
                    ),
                ),
            )
            request = RemoteSessionRequest(
                self.host,
                self.ssh,
                process,
                (StagedFile(config, PurePosixPath("environment.cfg")),),
                (),
            )
            session = RemoteSession.open(
                request,
                output_handler=lambda stream, payload, line_end: output.append(
                    (stream, payload, line_end)
                ),
            )
            try:
                assert session.wait_for_openocd_exit(timeout=30) == 0
            finally:
                session.close()
            assert any(
                payload == "ZRO_CONFIG_VALUE=channel_1" for _stream, payload, _line_end in output
            )
