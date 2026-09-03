# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import shlex
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path

from zephyr_remote_openocd.config import load_config, require_remote_settings
from zephyr_remote_openocd.remote import (
    RemoteProcess,
    RemoteSession,
    RemoteSessionRequest,
    Service,
    SshHelperBackend,
    StagedFile,
)
from zephyr_remote_openocd.remote.backend import SshHelperSession
from zephyr_remote_openocd.remote.deploy import deploy_helper
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.support import is_wsl2

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


def configured_host() -> str:
    host = os.environ.get("ZRO_SSH_TEST_HOST")
    if not host:
        raise unittest.SkipTest("ZRO_SSH_TEST_HOST is not configured")
    return host


def assert_remote_marker(test: unittest.TestCase, ssh: SshCommand, host: str):
    result = ssh.run(host, "printf zro-ssh-marker", timeout=20)
    test.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
    test.assertEqual(result.stdout, b"zro-ssh-marker")


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
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


class LinuxSshIntegrationTests(unittest.TestCase):
    def setUp(self):
        if is_wsl2():
            self.skipTest("native-Linux SSH test; WSL has dedicated coverage")
        self.host = configured_host()
        if shutil.which("ssh") is None:
            self.skipTest("Linux ssh is not available on PATH")

    def test_configured_linux_ssh_and_fixed_arguments(self):
        """Regression coverage for prototype gate PG-011."""
        assert_remote_marker(self, SshCommand(("ssh",)), self.host)
        assert_remote_marker(self, SshCommand(("ssh", "-o", "ConnectTimeout=10")), self.host)


class WslSshIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not is_wsl2():
            self.skipTest("PG-012/PG-013 require WSL 2; current host is not WSL 2")
        self.host = configured_host()

    def test_wsl_linux_ssh(self):
        """Deferred WSL regression coverage for prototype gate PG-012."""
        executable = shutil.which("ssh")
        if executable is None:
            self.skipTest("WSL distribution ssh is not available on PATH")
        assert_remote_marker(self, SshCommand((executable,)), self.host)

    def test_windows_ssh_exe_from_wsl(self):
        """Deferred WSL regression coverage for prototype gate PG-013."""
        configured = os.environ.get("ZRO_WINDOWS_SSH", "/mnt/c/Windows/System32/OpenSSH/ssh.exe")
        executable = Path(configured)
        if not executable.is_file():
            self.skipTest("Windows OpenSSH not found; set ZRO_WINDOWS_SSH to ssh.exe")
        assert_remote_marker(
            self,
            SshCommand((str(executable), "-o", "ControlMaster=no")),
            self.host,
        )


class SshTransportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.host = configured_host()
        if shutil.which("ssh") is None:
            self.skipTest("ssh is not available on PATH")
        self.ssh = SshCommand(("ssh", "-o", "ConnectTimeout=10"))

    def test_forwarding_does_not_require_controlmaster(self):
        """Regression coverage for prototype gate PG-014."""
        encoded = base64.b64encode(REMOTE_ECHO).decode("ascii")
        command = f"python3 -c \"import base64;exec(base64.b64decode('{encoded}'))\""
        controller = self.ssh.popen(self.host, command, "-o", "ControlMaster=no")
        tunnel = None
        try:
            assert controller.stdout is not None
            line = controller.stdout.readline()
            if not line:
                assert controller.stderr is not None
                self.fail(controller.stderr.read().decode(errors="replace"))
            remote_port = int(line)
            local_port = free_loopback_port()
            tunnel = self.ssh.popen(
                self.host,
                None,
                "-N",
                "-o",
                "ControlMaster=no",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            )
            wait_for_echo(local_port, b"zro-forwarding", 20)

            assert controller.stdin is not None
            controller.stdin.close()
            controller.wait(timeout=20)
            with self.assertRaises(AssertionError):
                wait_for_echo(local_port, b"must-not-echo", 1)
        finally:
            stop_and_close(controller)
            stop_and_close(tunnel)

    def test_streaming_preserves_content_and_reports_remote_failure(self):
        """Regression coverage for prototype gate PG-015."""
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
            with self.subTest(size=len(payload)):
                result = self.ssh.run(self.host, command, input_data=payload, timeout=30)
                expected = f"{len(payload)} {hashlib.sha256(payload).hexdigest()}\n".encode()
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(result.stdout, expected)

        failed = self.ssh.run(
            self.host,
            "python3 -c 'import sys; sys.stdin.buffer.read(); raise SystemExit(7)'",
            input_data=b"stream before remote failure",
            timeout=20,
        )
        self.assertEqual(failed.returncode, 7)

    def test_protocol_1_fake_helper_vertical_slice(self):
        """Permanent fake-workload coverage for the production transport path."""
        first = deploy_helper(self.ssh, self.host)
        second = deploy_helper(self.ssh, self.host)
        self.assertEqual(first.path, second.path)
        self.assertTrue(second.reused)
        local_port = free_loopback_port()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "payload.bin"
            source.write_bytes(bytes(range(256)) + b"\0remote")
            request = RemoteSessionRequest(
                self.host,
                self.ssh,
                (StagedFile(source, "nested/payload.bin"),),
                (Service("gdb", local_port, 3333),),
            )
            session = RemoteSession(request, SshHelperBackend())
            descriptor = session.start()
            try:
                address = ipaddress.ip_address(descriptor.remote_address)
                self.assertIn(address, ipaddress.ip_network("127.64.0.0/10"))
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
                self.assertEqual(check.returncode, 0, check.stderr.decode(errors="replace"))
                self.assertEqual(check.stdout.strip(), b"0o700 263")
                wait_for_echo(local_port, b"fake-helper-round-trip", 20)
            finally:
                workspace = descriptor.remote_workspace
                session.close()
            gone = self.ssh.run(self.host, f"test ! -e {shlex.quote(workspace)}", timeout=20)
            self.assertEqual(gone.returncode, 0, gone.stderr.decode(errors="replace"))

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
            self.assertNotEqual(
                first_descriptor.remote_address,
                second_descriptor.remote_address,
            )
            self.assertNotEqual(first_descriptor.session_id, second_descriptor.session_id)
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
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_controller_ssh_loss_cleans_fake_session(self):
        """Regression coverage for controller-loss cleanup semantics."""
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
            self.assertIsInstance(backend_session, SshHelperSession)
            assert isinstance(backend_session, SshHelperSession)
            backend_session.controller.terminate()
            backend_session.controller.wait(timeout=20)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and session.poll() is None:
                time.sleep(0.1)
            self.assertIsNotNone(session.termination_returncode)
        finally:
            session.close()
        result = self.ssh.run(
            self.host,
            f"test ! -e {shlex.quote(descriptor.remote_workspace)}",
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_remote_openocd_config_consumes_forwarded_environment(self):
        """Verify allow-listed environment reaches remote OpenOCD Tcl config."""
        config_path = os.environ.get("ZRO_SSH_TEST_CONFIG")
        if config_path:
            configured_host, executable = require_remote_settings(
                load_config(Path(config_path)), "environment-forwarding test"
            )
            if configured_host != self.host:
                self.fail("ZRO_SSH_TEST_HOST does not match remote.host in ZRO_SSH_TEST_CONFIG")
        else:
            executable = os.environ.get("ZRO_SSH_TEST_OPENOCD")
        if not executable:
            self.skipTest("ZRO_SSH_TEST_CONFIG or ZRO_SSH_TEST_OPENOCD is not configured")
        if not executable.startswith("/"):
            self.fail("ZRO_SSH_TEST_OPENOCD must be an absolute remote path")
        executable_result = self.ssh.run(
            self.host, f"test -x {shlex.quote(executable)}", timeout=20
        )
        if executable_result.returncode:
            self.skipTest(f"remote OpenOCD is not executable: {executable}")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "environment.cfg"
            config.write_text(
                "set zro_forwarded_value $::env(ZRO_CONFIG_VALUE)\n"
                "echo ZRO_CONFIG_VALUE=$zro_forwarded_value\n"
                "shutdown\n"
            )
            output = []
            process = RemoteProcess(
                "openocd",
                (executable, "-f", "{workspace}/staged/environment.cfg"),
                (("ZRO_CONFIG_VALUE", "channel-1"),),
            )
            request = RemoteSessionRequest(
                self.host,
                self.ssh,
                (StagedFile(config, "environment.cfg"),),
                (),
                process,
            )
            session = RemoteSession(
                request,
                SshHelperBackend(
                    output_handler=lambda stream, payload: output.append((stream, payload))
                ),
            )
            try:
                session.start()
                self.assertEqual(session.wait(timeout=30), 0)
            finally:
                session.close()
            self.assertTrue(
                any(payload == "ZRO_CONFIG_VALUE=channel-1" for _stream, payload in output)
            )


if __name__ == "__main__":
    unittest.main()
