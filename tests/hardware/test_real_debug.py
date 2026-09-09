# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import time

import pytest
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.hardware_support import elf_memory_witness
from tests.process_support import read_line
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
            "remote_openocd",
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

    def test_debug(self, debug_fixture):
        self._debug(debug_fixture)

    def test_attach(self, attach_fixture):
        self._attach(attach_fixture)

    def test_debugserver(self, debugserver_fixture):
        self._debugserver(debugserver_fixture)

    def _debug(self, fixture):
        breakpoint = fixture["debug_breakpoint"]
        commands = (
            f"break {breakpoint}",
            "continue",
            'printf "ZRO_PC_BEGIN\\n"',
            "p/x $pc",
            'printf "ZRO_PC_END\\n"',
            'printf "ZRO_INSN_BEGIN\\n"',
            "x/1i $pc",
            'printf "ZRO_INSN_END\\n"',
            "detach",
            "quit",
        )
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
        assert result.returncode == 0, result.stdout
        assert re.search(rf"Breakpoint \d+,\s+{re.escape(breakpoint)}\b", result.stdout)
        assert re.search(r"ZRO_PC_BEGIN\s*\$\d+\s*=\s*0x[0-9a-fA-F]+", result.stdout)
        assert re.search(r"ZRO_INSN_BEGIN\s*=>\s*0x[0-9a-fA-F]+", result.stdout)
        assert "ZRO_PC_END" in result.stdout
        assert "ZRO_INSN_END" in result.stdout
        for pattern in fixture.get("debug_patterns", ()):
            assert re.search(pattern, result.stdout)
        self._assert_cleanup(fixture, result.stdout)

    def _attach(self, fixture):
        prepared = subprocess.run(
            self._west_command(fixture, "flash", fixture["attach_precondition_build_dir"]),
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
        address, precondition_bytes, selected_bytes = elf_memory_witness(
            fixture["attach_precondition_elf_file"], fixture["elf_file"]
        )
        command = self._west_command(
            fixture,
            "attach",
            extra_args=(
                '--gdb-init=printf "ZRO_PC_BEGIN\\n"',
                "--gdb-init=p/x $pc",
                '--gdb-init=printf "ZRO_PC_END\\n"',
                '--gdb-init=printf "ZRO_INSN_BEGIN\\n"',
                "--gdb-init=x/1i $pc",
                '--gdb-init=printf "ZRO_INSN_END\\n"',
                '--gdb-init=printf "ZRO_IMAGE_BEGIN\\n"',
                f"--gdb-init=x/{len(precondition_bytes)}bx 0x{address:x}",
                '--gdb-init=printf "ZRO_IMAGE_END\\n"',
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
        assert re.search(r"ZRO_PC_BEGIN\s*\$\d+\s*=\s*0x[0-9a-fA-F]+", result.stdout)
        assert re.search(r"ZRO_INSN_BEGIN\s*=>?\s*0x[0-9a-fA-F]+", result.stdout)
        assert "ZRO_PC_END" in result.stdout
        assert "ZRO_INSN_END" in result.stdout
        image_match = re.search(
            r"ZRO_IMAGE_BEGIN\s*(.*?)\s*ZRO_IMAGE_END", result.stdout, re.DOTALL
        )
        assert image_match is not None, result.stdout
        observed = bytes(
            int(value, 16)
            for line in image_match.group(1).splitlines()
            for value in re.findall(r"0x([0-9a-fA-F]{2})\b", line.partition(":")[2])
        )
        assert observed == precondition_bytes
        assert observed != selected_bytes
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
            end = time.monotonic() + float(fixture.get("startup_timeout", 90))
            session = None
            while time.monotonic() < end and session is None:
                line = read_line(process.stdout, end - time.monotonic()).decode("utf-8", "replace")
                if not line:
                    break
                output.append(line)
                session = SESSION_PATTERN.search(line)
            assert session is not None, "".join(output)
            assert process.poll() is None, "debugserver exited before client connection"
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
            "flash",
            fixture["thread_build_dir"],
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
        assert re.search(fixture["thread_info_pattern"], result.stdout)
        self._assert_cleanup(fixture, result.stdout)
