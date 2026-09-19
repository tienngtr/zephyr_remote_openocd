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
    SshHelperBackend,
    StagedFile,
)
from zephyr_remote_openocd.remote.backend import SshHelperSession
from zephyr_remote_openocd.remote.deploy import deploy_helper
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.process_support import read_line

pytestmark = pytest.mark.ssh

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
            tunnel = self.ssh.popen(
                self.host,
                None,
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            )
            wait_for_echo(local_port, b"zro_forwarding", 20)

            assert helper_process.stdin is not None
            helper_process.stdin.close()
            helper_process.wait(timeout=20)
            with pytest.raises(AssertionError):
                wait_for_echo(local_port, b"must_not_echo", 1)
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
            "python3 -c 'import sys; sys.stdin.buffer.read(); raise SystemExit(7)'",
            input_data=b"stream before remote failure",
            timeout=20,
        )
        assert failed.returncode == 7

    def test_protocol_v1_fake_helper_vertical_slice(self):
        """Permanent fake-workload coverage for the production transport path."""
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
                (StagedFile(source, PurePosixPath("nested/payload.bin")),),
                (Service("gdb", local_port, 3333),),
            )
            session = RemoteSession(request, SshHelperBackend())
            descriptor = session.start()
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
                wait_for_echo(local_port, b"fake_helper_round_trip", 20)
            finally:
                workspace = descriptor.remote_workspace
                session.close()
            gone = self.ssh.run(self.host, f"test ! -e {shlex.quote(workspace)}", timeout=20)
            assert gone.returncode == 0, gone.stderr.decode(errors="replace")

    def test_concurrent_fake_sessions_isolate_identical_remote_ports(self):
        """Regression coverage for independent-session service-port isolation."""
        first_port = free_loopback_port()
        second_port = free_loopback_port()
        while second_port == first_port:
            second_port = free_loopback_port()
        first = RemoteSession(
            RemoteSessionRequest(
                self.host,
                self.ssh,
                services=(Service("gdb", first_port, 3333),),
            ),
            SshHelperBackend(),
        )
        second = RemoteSession(
            RemoteSessionRequest(
                self.host,
                self.ssh,
                services=(Service("gdb", second_port, 3333),),
            ),
            SshHelperBackend(),
        )
        first_descriptor = first.start()
        second_descriptor = None
        try:
            second_descriptor = second.start()
            assert first_descriptor.remote_address != second_descriptor.remote_address
            assert first_descriptor.session_id != second_descriptor.session_id
        finally:
            second.close()
            first.close()
        assert second_descriptor is not None
        for descriptor in (first_descriptor, second_descriptor):
            result = self.ssh.run(
                self.host,
                f"test ! -e {shlex.quote(descriptor.remote_workspace)}",
                timeout=20,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")

    def test_helper_ssh_loss_cleans_fake_session(self):
        """Losing the helper SSH process cleans the fake session."""
        local_port = free_loopback_port()
        session = RemoteSession(
            RemoteSessionRequest(
                self.host,
                self.ssh,
                services=(Service("gdb", local_port, 3333),),
            ),
            SshHelperBackend(),
        )
        descriptor = session.start()
        try:
            backend_session = session._session
            assert isinstance(backend_session, SshHelperSession)
            backend_session.helper_process.terminate()
            backend_session.helper_process.wait(timeout=20)
            with pytest.raises(SessionError):
                session.wait(timeout=20)
        finally:
            session.close()
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
            )
            request = RemoteSessionRequest(
                self.host,
                self.ssh,
                (StagedFile(config, PurePosixPath("environment.cfg")),),
                (),
                process,
            )
            session = RemoteSession(
                request,
                SshHelperBackend(
                    output_handler=lambda stream, payload, line_end: output.append(
                        (stream, payload, line_end)
                    )
                ),
            )
            try:
                session.start()
                assert session.wait(timeout=30) == 0
            finally:
                session.close()
            assert any(
                payload == "ZRO_CONFIG_VALUE=channel_1" for _stream, payload, _line_end in output
            )
