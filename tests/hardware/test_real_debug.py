from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import time
import unittest

from tests.hardware.test_real_flash import SERIAL_READER, _read_event, _stop
from tests.support import ROOT
from zephyr_remote_openocd.remote.ssh import SshCommand


SESSION_PATTERN = re.compile(r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)")


class RealOpenOcdDebugTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = os.environ.get("ZRO_REAL_DEBUG_FIXTURES")
        if not fixture_path:
            raise unittest.SkipTest("ZRO_REAL_DEBUG_FIXTURES is not configured")
        try:
            cls.fixtures = list(json.loads(Path(fixture_path).read_text())["fixtures"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid real-debug fixture file {fixture_path}: {error}") from error
        if not cls.fixtures:
            raise unittest.SkipTest("real-debug fixture file configures no fixtures")
        if not any(item.get("supports_thread_info") for item in cls.fixtures):
            raise RuntimeError("at least one real-debug fixture must support thread info")

    def _environment(self, fixture):
        environment = os.environ.copy()
        environment.pop("ZEPHYR_REMOTE_OPENOCD_RECORD", None)
        environment.update({
            "EXTRA_ZEPHYR_MODULES": str(ROOT),
            "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(fixture["config_path"]),
        })
        environment.update({
            str(key): str(value) for key, value in fixture.get("environment", {}).items()
        })
        return environment

    def _west_command(self, fixture, command, build_dir=None, extra_args=()):
        west = fixture.get("west") or shutil.which("west")
        if not west:
            self.fail("fixture has no west executable and west is not on PATH")
        result = [
            str(west), command, "-d", str(build_dir or fixture["build_dir"]),
            "-r", "remote-openocd", "--no-rebuild",
        ]
        runner_args = [*fixture.get("debug_runner_args", ()), *extra_args]
        if runner_args:
            result.extend(("--", *map(str, runner_args)))
        return result

    def _assert_cleanup(self, fixture, output):
        session = SESSION_PATTERN.search(output)
        self.assertIsNotNone(session, output)
        assert session is not None
        ssh = SshCommand(tuple(fixture["ssh_command"]))
        cleanup = ssh.run(
            fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20
        )
        self.assertEqual(cleanup.returncode, 0, cleanup.stderr.decode("utf-8", "replace"))

    def _serial_reader(self, fixture):
        ssh = SshCommand(tuple(fixture["ssh_command"]))
        encoded = base64.b64encode(SERIAL_READER.encode()).decode("ascii")
        remote_command = " ".join((
            "python3", "-c",
            shlex.quote(f"import base64;exec(base64.b64decode('{encoded}'))"),
            shlex.quote(str(fixture["serial_device"])),
            shlex.quote(str(fixture["serial_baud"])),
            shlex.quote(str(fixture["expected_pattern"])),
            shlex.quote(str(fixture["serial_timeout"])),
        ))
        return ssh.popen(fixture["host"], remote_command, "-o", "ControlMaster=no")

    def test_debug_attach_and_debugserver(self):
        for fixture in self.fixtures:
            with self.subTest(fixture=fixture.get("id"), command="debug"):
                self._debug(fixture)
            with self.subTest(fixture=fixture.get("id"), command="attach"):
                self._attach(fixture)
            with self.subTest(fixture=fixture.get("id"), command="debugserver"):
                self._debugserver(fixture)

    def _debug(self, fixture):
        reader = self._serial_reader(fixture)
        try:
            self.assertEqual(_read_event(reader, 15)["type"], "READY")
            assert reader.stdin is not None
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()
            commands = fixture.get("debug_gdb_init", ("monitor reset run", "detach", "quit"))
            command = self._west_command(
                fixture, "debug", extra_args=tuple(f"--gdb-init={item}" for item in commands)
            )
            result = subprocess.run(
                command, cwd=fixture.get("workspace"), env=self._environment(fixture),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=180,
            )
            event = _read_event(reader, float(fixture["serial_timeout"]) + 2)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(event["type"], "MATCH", event)
            self.assertIn("Loading section", result.stdout)
            self.assertIn("Remote debugging using 127.0.0.1:", result.stdout)
            for pattern in fixture.get("debug_patterns", ()):
                self.assertRegex(result.stdout, pattern)
            self._assert_cleanup(fixture, result.stdout)
        finally:
            _stop(reader)

    def _attach(self, fixture):
        command = self._west_command(fixture, "attach", extra_args=(
            "--gdb-init=detach", "--gdb-init=quit",
        ))
        result = subprocess.run(
            command, cwd=fixture.get("workspace"), env=self._environment(fixture),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Remote debugging using 127.0.0.1:", result.stdout)
        self.assertNotIn("Loading section", result.stdout)
        self._assert_cleanup(fixture, result.stdout)

    def _debugserver(self, fixture):
        process = subprocess.Popen(
            self._west_command(fixture, "debugserver"),
            cwd=fixture.get("workspace"), env=self._environment(fixture),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        output = []
        try:
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            end = time.monotonic() + float(fixture.get("startup_timeout", 90))
            session = None
            while time.monotonic() < end and session is None:
                if not selector.select(min(1, max(0, end - time.monotonic()))):
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                output.append(line)
                session = SESSION_PATTERN.search(line)
            self.assertIsNotNone(session, "".join(output))
            self.assertIsNone(process.poll(), "debugserver exited before client connection")
            self.assertNotIn("GNU gdb", "".join(output))
            for port in fixture.get("enabled_local_ports", (6333, 4444)):
                with socket.create_connection(("127.0.0.1", int(port)), timeout=5):
                    pass
            gdb_port = int(fixture.get("gdb_client_port", 3333))
            client = subprocess.run(
                [
                    fixture["gdb"], "-q", "-batch", fixture["elf_file"],
                    "-ex", f"target extended-remote 127.0.0.1:{gdb_port}",
                    "-ex", "monitor halt", "-ex", "monitor resume",
                    "-ex", "detach", "-ex", "quit",
                ],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                check=False, timeout=30,
            )
            self.assertEqual(client.returncode, 0, client.stdout)
            self.assertIsNone(process.poll(), "debugserver did not remain persistent")
            process.send_signal(signal.SIGINT)
            remainder, _ = process.communicate(timeout=15)
            output.append(remainder)
            self._assert_cleanup(fixture, "".join(output))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def test_thread_info_on_capable_fixtures(self):
        for fixture in self.fixtures:
            if not fixture.get("supports_thread_info"):
                continue
            with self.subTest(fixture=fixture.get("id")):
                prepare = self._west_command(
                    fixture, "debug", fixture["thread_build_dir"],
                    ("--gdb-init=monitor reset run", "--gdb-init=detach", "--gdb-init=quit"),
                )
                prepared = subprocess.run(
                    prepare, cwd=fixture.get("workspace"), env=self._environment(fixture),
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False, timeout=180,
                )
                self.assertEqual(prepared.returncode, 0, prepared.stdout)
                self._assert_cleanup(fixture, prepared.stdout)
                command = self._west_command(
                    fixture, "attach", fixture["thread_build_dir"],
                    ("--gdb-init=info threads", "--gdb-init=detach", "--gdb-init=quit"),
                )
                result = subprocess.run(
                    command, cwd=fixture.get("workspace"), env=self._environment(fixture),
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    check=False, timeout=180,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("Zephyr: target known", result.stdout)
                self.assertRegex(result.stdout, fixture["thread_info_pattern"])
                self._assert_cleanup(fixture, result.stdout)


if __name__ == "__main__":
    unittest.main()
