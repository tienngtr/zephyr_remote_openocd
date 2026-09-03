# SPDX-License-Identifier: Apache-2.0

"""Fixture-gated destructive acceptance tests for real RTT transport."""

from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import time
import unittest
from pathlib import Path

from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.hardware.test_real_debug import SESSION_PATTERN
from tests.support import ROOT

RTT_ENDPOINT_PATTERN = re.compile(r"RTT server available at 127\.0\.0\.1:(\d+)")


class RealRttTests(unittest.TestCase):
    """Validate channel-0 RTT and the two persistent server variants."""

    @classmethod
    def setUpClass(cls):
        fixture_path = os.environ.get("ZRO_REAL_RTT_FIXTURES")
        if not fixture_path:
            raise unittest.SkipTest("ZRO_REAL_RTT_FIXTURES is not configured")
        try:
            fixtures = json.loads(Path(fixture_path).read_text())["fixtures"]
            cls.fixtures = [item for item in fixtures if item.get("supports_rtt")]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid real-RTT fixture file {fixture_path}: {error}") from error
        if not cls.fixtures:
            raise unittest.SkipTest("real-RTT fixture file configures no RTT-capable fixtures")

    def _environment(self, fixture):
        environment = os.environ.copy()
        environment.pop("ZEPHYR_REMOTE_OPENOCD_RECORD", None)
        environment.update(
            {
                "EXTRA_ZEPHYR_MODULES": str(ROOT),
                "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(fixture["config_path"]),
            }
        )
        environment.update(
            {str(key): str(value) for key, value in fixture.get("environment", {}).items()}
        )
        return environment

    def _assert_cleanup(self, fixture, output):
        session = SESSION_PATTERN.search(output)
        self.assertIsNotNone(session, output)
        assert session is not None
        result = SshCommand(tuple(fixture["ssh_command"])).run(
            fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    @staticmethod
    def _west_command(fixture, command, *runner_args):
        west = fixture.get("west") or shutil.which("west")
        if not west:
            raise AssertionError("fixture has no west executable and west is not on PATH")
        return [
            str(west),
            command,
            "-d",
            str(fixture["rtt_build_dir"]),
            "-r",
            "remote-openocd",
            "--no-rebuild",
            "--",
            *map(str, fixture.get("rtt_runner_args", ())),
            *map(str, runner_args),
        ]

    @staticmethod
    def _read_until(process, pattern, timeout, output):
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not selector.select(min(0.5, max(0, deadline - time.monotonic()))):
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            output.extend(chunk)
            if re.search(pattern.encode(), bytes(output)):
                return
        raise AssertionError(
            f"pattern {pattern!r} not observed; status={process.poll()}:\n"
            + bytes(output).decode("utf-8", "replace")
        )

    def _start(self, fixture, command, *runner_args):
        return subprocess.Popen(
            self._west_command(fixture, command, *runner_args),
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _program(self, fixture):
        result = subprocess.run(
            self._west_command(fixture, "flash"),
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self._assert_cleanup(fixture, result.stdout)

    def _finish(self, fixture, process, output, *, interrupt=False):
        if process.poll() is None and interrupt:
            process.send_signal(signal.SIGINT)
        try:
            remainder, _ = process.communicate(timeout=20)
            output.extend(remainder)
        except subprocess.TimeoutExpired:
            process.kill()
            output.extend(process.communicate()[0])
            self.fail("RTT west process did not terminate")
        text = bytes(output).decode("utf-8", "replace")
        self._assert_cleanup(fixture, text)
        return text

    @staticmethod
    def _abort(process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _rtt_round_trip(self, fixture, port):
        with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
            connection.settimeout(1)
            request = str(fixture.get("rtt_input", "help\n")).encode()
            expected = str(fixture["expected_rtt_response"]).encode()
            received = bytearray()
            deadline = time.monotonic() + float(fixture.get("rtt_timeout", 30))
            next_send = 0.0
            while expected not in received and time.monotonic() < deadline:
                if time.monotonic() >= next_send:
                    connection.sendall(request)
                    next_send = time.monotonic() + 1
                try:
                    received.extend(connection.recv(4096))
                except TimeoutError:
                    continue
            self.assertIn(expected, received, received.decode("utf-8", "replace"))

    def test_standalone_rtt(self):
        for fixture in self.fixtures:
            with self.subTest(fixture=fixture.get("id")):
                self._program(fixture)
                port = int(fixture["rtt_port"])
                process = self._start(fixture, "rtt", f"--rtt-port={port}")
                output = bytearray()
                try:
                    self._read_until(
                        process,
                        RTT_ENDPOINT_PATTERN.pattern,
                        90,
                        output,
                    )
                    self.assertIsNone(process.poll())
                    self.assertIn(f"127.0.0.1:{port}".encode(), output)
                    time.sleep(1)
                    assert process.stdin is not None
                    process.stdin.write(str(fixture.get("rtt_input", "help\n")).encode())
                    process.stdin.flush()
                    self._read_until(
                        process,
                        str(fixture["expected_rtt_response"]),
                        float(fixture.get("rtt_timeout", 30)),
                        output,
                    )
                finally:
                    text = self._finish(fixture, process, output, interrupt=True)
                self.assertRegex(text, SESSION_PATTERN)

    def test_debug_rtt_server_keeps_gdb_foreground(self):
        for fixture in self.fixtures:
            with self.subTest(fixture=fixture.get("id")):
                self._program(fixture)
                port = int(fixture["rtt_port"])
                process = self._start(
                    fixture,
                    "debug",
                    "--rtt-server",
                    f"--rtt-port={port}",
                    "--gdb-init=monitor reset run",
                    "--gdb-init=echo ZRO_GDB_RTT_READY\\n",
                    "--gdb-init=shell sleep 15",
                    "--gdb-init=detach",
                    "--gdb-init=quit",
                )
                output = bytearray()
                try:
                    self._read_until(process, RTT_ENDPOINT_PATTERN.pattern, 90, output)
                    self._read_until(process, "ZRO_GDB_RTT_READY", 90, output)
                    self.assertIsNone(process.poll())
                    try:
                        self._rtt_round_trip(fixture, port)
                    except (AssertionError, OSError) as error:
                        text = self._finish(fixture, process, output, interrupt=True)
                        self.fail(f"{error}\n{text}")
                    text = self._finish(fixture, process, output)
                finally:
                    self._abort(process)
                self.assertIn("GNU gdb", text)

    def test_debugserver_exposes_gdb_and_rtt_without_clients(self):
        for fixture in self.fixtures:
            with self.subTest(fixture=fixture.get("id")):
                self._program(fixture)
                port = int(fixture["rtt_port"])
                process = self._start(
                    fixture,
                    "debugserver",
                    "--rtt-server",
                    f"--rtt-port={port}",
                )
                output = bytearray()
                try:
                    self._read_until(process, RTT_ENDPOINT_PATTERN.pattern, 90, output)
                    self.assertNotIn(b"GNU gdb", output)
                    gdb_port = int(fixture.get("gdb_client_port", 3333))
                    client = subprocess.run(
                        [
                            str(fixture["gdb"]),
                            "-q",
                            "-batch",
                            str(fixture["rtt_elf_file"]),
                            "-ex",
                            f"target extended-remote 127.0.0.1:{gdb_port}",
                            "-ex",
                            "load",
                            "-ex",
                            "monitor reset run",
                            "-ex",
                            "detach",
                            "-ex",
                            "quit",
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        check=False,
                        timeout=30,
                    )
                    self.assertEqual(client.returncode, 0, client.stdout)
                    self._rtt_round_trip(fixture, port)
                    text = self._finish(fixture, process, output, interrupt=True)
                finally:
                    self._abort(process)
                self.assertNotIn("GNU gdb", text)


if __name__ == "__main__":
    unittest.main()
