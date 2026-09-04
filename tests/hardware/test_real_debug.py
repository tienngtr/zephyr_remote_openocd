# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import time

import pytest
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.serial_reader import (
    read_event as _read_event,
)
from tests.serial_reader import (
    remote_serial_reader_command,
)
from tests.serial_reader import (
    stop_reader as _stop,
)
from tests.support import ROOT

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]

SESSION_PATTERN = re.compile(r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)")


class TestRealOpenOcdDebug:
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

    def _west_command(self, fixture, command, build_dir=None, extra_args=()):
        west = fixture.get("west") or shutil.which("west")
        if not west:
            pytest.fail("fixture has no west executable and west is not on PATH")
        result = [
            str(west),
            command,
            "-d",
            str(build_dir or fixture["build_dir"]),
            "-r",
            "remote-openocd",
            "--no-rebuild",
        ]
        runner_args = [*fixture.get("debug_runner_args", ()), *extra_args]
        if runner_args:
            result.extend(("--", *map(str, runner_args)))
        return result

    def _assert_cleanup(self, fixture, output):
        session = SESSION_PATTERN.search(output)
        assert session is not None, output
        ssh = SshCommand(tuple(fixture["ssh_command"]))
        cleanup = ssh.run(fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20)
        assert cleanup.returncode == 0, cleanup.stderr.decode("utf-8", "replace")

    def _serial_reader(self, fixture):
        ssh = SshCommand(tuple(fixture["ssh_command"]))
        remote_command = remote_serial_reader_command(
            str(fixture["serial_device"]),
            int(fixture["serial_baud"]),
            str(fixture["expected_pattern"]),
            float(fixture["serial_timeout"]),
            data_bits=int(fixture.get("serial_data_bits", 8)),
            parity=str(fixture.get("serial_parity", "none")),
            stop_bits=int(fixture.get("serial_stop_bits", 1)),
            flow_control=str(fixture.get("serial_flow_control", "none")),
        )
        return ssh.popen(fixture["host"], remote_command, "-o", "ControlMaster=no")

    def test_debug(self, debug_fixture):
        self._debug(debug_fixture)

    def test_attach(self, attach_fixture):
        self._attach(attach_fixture)

    def test_debugserver(self, debugserver_fixture):
        self._debugserver(debugserver_fixture)

    def _debug(self, fixture):
        reader = self._serial_reader(fixture)
        try:
            assert _read_event(reader, 15)["type"] == "READY"
            assert reader.stdin is not None
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()
            commands = fixture.get("debug_gdb_init", ("monitor reset run", "detach", "quit"))
            command = self._west_command(
                fixture, "debug", extra_args=tuple(f"--gdb-init={item}" for item in commands)
            )
            result = subprocess.run(
                command,
                cwd=fixture.get("workspace"),
                env=self._environment(fixture),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=180,
            )
            event = _read_event(reader, float(fixture["serial_timeout"]) + 2)
            assert result.returncode == 0, result.stdout
            assert event["type"] == "MATCH", event
            assert "Loading section" in result.stdout
            assert "Remote debugging using 127.0.0.1:" in result.stdout
            for pattern in fixture.get("debug_patterns", ()):
                assert re.search(pattern, result.stdout)
            self._assert_cleanup(fixture, result.stdout)
        finally:
            _stop(reader)

    def _attach(self, fixture):
        command = self._west_command(
            fixture,
            "attach",
            extra_args=(
                "--gdb-init=detach",
                "--gdb-init=quit",
            ),
        )
        result = subprocess.run(
            command,
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout
        assert "Remote debugging using 127.0.0.1:" in result.stdout
        assert "Loading section" not in result.stdout
        self._assert_cleanup(fixture, result.stdout)

    def _debugserver(self, fixture):
        process = subprocess.Popen(
            self._west_command(fixture, "debugserver"),
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
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
            assert session is not None, "".join(output)
            assert process.poll() is None, "debugserver exited before client connection"
            assert "GNU gdb" not in "".join(output)
            for port in fixture.get("enabled_local_ports", (6333, 4444)):
                with socket.create_connection(("127.0.0.1", int(port)), timeout=5):
                    pass
            gdb_port = int(fixture.get("gdb_client_port", 3333))
            client = subprocess.run(
                [
                    fixture["gdb"],
                    "-q",
                    "-batch",
                    fixture["elf_file"],
                    "-ex",
                    f"target extended-remote 127.0.0.1:{gdb_port}",
                    "-ex",
                    "monitor halt",
                    "-ex",
                    "monitor resume",
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
            assert client.returncode == 0, client.stdout
            assert process.poll() is None, "debugserver did not remain persistent"
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

    def test_thread_info_on_capable_fixture(self, thread_info_fixture):
        fixture = thread_info_fixture
        prepare = self._west_command(
            fixture,
            "debug",
            fixture["thread_build_dir"],
            ("--gdb-init=monitor reset run", "--gdb-init=detach", "--gdb-init=quit"),
        )
        prepared = subprocess.run(
            prepare,
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert prepared.returncode == 0, prepared.stdout
        self._assert_cleanup(fixture, prepared.stdout)
        command = self._west_command(
            fixture,
            "attach",
            fixture["thread_build_dir"],
            ("--gdb-init=info threads", "--gdb-init=detach", "--gdb-init=quit"),
        )
        result = subprocess.run(
            command,
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout
        assert "Zephyr: target known" in result.stdout
        assert re.search(fixture["thread_info_pattern"], result.stdout)
        self._assert_cleanup(fixture, result.stdout)
