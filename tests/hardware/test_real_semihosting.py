# SPDX-License-Identifier: Apache-2.0

"""Fixture-gated direct-semihosting console acceptance tests."""

from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import time
import unittest
from pathlib import Path

from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.support import ROOT

SESSION_PATTERN = re.compile(r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)")


class RealSemihostingTests(unittest.TestCase):
    """Validate direct semihosting through the normal OpenOCD output relay."""

    @classmethod
    def setUpClass(cls):
        fixture_path = os.environ.get("ZRO_REAL_SEMIHOSTING_FIXTURES")
        if not fixture_path:
            raise unittest.SkipTest("ZRO_REAL_SEMIHOSTING_FIXTURES is not configured")
        try:
            fixtures = list(json.loads(Path(fixture_path).read_text())["fixtures"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"invalid real-semihosting fixture file {fixture_path}: {error}"
            ) from error
        if not fixtures:
            raise unittest.SkipTest("semihosting fixture file configures no fixtures")
        cls.fixtures = fixtures
        if not any(item.get("supports_semihosting") for item in fixtures):
            raise RuntimeError("at least one semihosting fixture must support semihosting")

    def _capable(self):
        return [item for item in self.fixtures if item.get("supports_semihosting")]

    @staticmethod
    def _environment(fixture):
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

    @staticmethod
    def _west(fixture, command, *, gdb_init=()):
        west = fixture.get("west") or shutil.which("west")
        if not west:
            raise AssertionError("fixture has no west executable and west is not on PATH")
        args = [
            str(west),
            command,
            "-d",
            str(fixture["build_dir"]),
            "-r",
            "remote-openocd",
            "--no-rebuild",
        ]
        runner_args = [str(item) for item in fixture.get("runner_args", ())]
        if command == "debug":
            runner_args.extend(f"--cmd-pre-init={item}" for item in fixture["semihosting_commands"])
            runner_args.extend(f"--gdb-init={item}" for item in gdb_init)
        if runner_args:
            args.extend(("--", *runner_args))
        return args

    def _assert_cleanup(self, fixture, output):
        session = SESSION_PATTERN.search(output)
        self.assertIsNotNone(session, output)
        assert session is not None
        result = SshCommand(tuple(fixture["ssh_command"])).run(
            fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def _flash(self, fixture):
        result = subprocess.run(
            self._west(fixture, "flash"),
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=240,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self._assert_cleanup(fixture, result.stdout)

    def test_direct_semihosting_console_normal_completion(self):
        for fixture in self._capable():
            with self.subTest(fixture=fixture.get("id")):
                self._flash(fixture)
                gdb_init = fixture.get(
                    "normal_gdb_init",
                    ("monitor resume", "shell sleep 2", "monitor halt", "detach", "quit"),
                )
                command = self._west(fixture, "debug", gdb_init=gdb_init)
                self.assertNotIn("--no-load", command)
                process = subprocess.Popen(
                    command,
                    cwd=fixture.get("workspace"),
                    env=self._environment(fixture),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                output = bytearray()
                try:
                    assert process.stdout is not None
                    selector = selectors.DefaultSelector()
                    selector.register(process.stdout, selectors.EVENT_READ)
                    deadline = time.monotonic() + float(fixture.get("timeout", 30))
                    while time.monotonic() < deadline:
                        if not selector.select(0.5):
                            if process.poll() is not None:
                                break
                            continue
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if re.search(fixture["expected_output"].encode(), output):
                            break
                    self.assertRegex(
                        bytes(output).decode("utf-8", "replace"), fixture["expected_output"]
                    )
                    if process.poll() is None:
                        # Allow the fixture's GDB sequence to finish its
                        # orderly halt/detach after the oracle sees output.
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGTERM)
                    try:
                        remainder, _ = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        remainder, _ = process.communicate(timeout=10)
                    output.extend(remainder)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None and not stream.closed:
                            stream.close()
                self._assert_cleanup(fixture, bytes(output).decode("utf-8", "replace"))

    def test_direct_semihosting_console_interruption(self):
        for fixture in self._capable():
            with self.subTest(fixture=fixture.get("id")):
                self._flash(fixture)
                gdb_init = fixture.get(
                    "interrupt_gdb_init",
                    ("monitor reset run", "shell sleep 30"),
                )
                command = self._west(fixture, "debug", gdb_init=gdb_init)
                self.assertNotIn("--no-load", command)
                process = subprocess.Popen(
                    command,
                    cwd=fixture.get("workspace"),
                    env=self._environment(fixture),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                output = bytearray()
                try:
                    assert process.stdout is not None
                    selector = selectors.DefaultSelector()
                    selector.register(process.stdout, selectors.EVENT_READ)
                    deadline = time.monotonic() + float(fixture.get("timeout", 30))
                    while time.monotonic() < deadline:
                        if not selector.select(0.5):
                            if process.poll() is not None:
                                break
                            continue
                        chunk = os.read(process.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if re.search(fixture["expected_output"].encode(), output):
                            break
                    self.assertRegex(
                        bytes(output).decode("utf-8", "replace"), fixture["expected_output"]
                    )
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                    try:
                        remainder, _ = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            remainder, _ = process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            remainder, _ = process.communicate(timeout=10)
                    output.extend(remainder)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None and not stream.closed:
                            stream.close()
                self._assert_cleanup(fixture, bytes(output).decode("utf-8", "replace"))

    def test_no_semihosting_specific_transport_or_file_io_path(self):
        production = "\n".join(
            path.read_text() for path in (ROOT / "python" / "zephyr_remote_openocd").rglob("*.py")
        ).lower()
        self.assertNotIn("semihostingproxy", production)
        self.assertNotIn("file_io", production)
        self.assertNotIn("semihosting_socket", production)


if __name__ == "__main__":
    unittest.main()
