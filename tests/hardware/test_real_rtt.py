# SPDX-License-Identifier: Apache-2.0

"""Fixture-gated destructive acceptance tests for real RTT transport."""

from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import time

import pytest
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.hardware.test_real_debug import SESSION_PATTERN
from tests.hardware_support import RttFixture
from tests.process_support import read_until
from tests.support import ROOT

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]

RTT_ENDPOINT_PATTERN = re.compile(r"RTT server available at 127\.0\.0\.1:(\d+)")


class TestRealRtt:
    """Validate channel-0 RTT and the two persistent server variants."""

    def _environment(self, fixture: RttFixture) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("ZRO_RECORD", None)
        environment.update(
            {
                "EXTRA_ZEPHYR_MODULES": str(ROOT),
                "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(fixture.target.config_path),
            }
        )
        environment.update(dict(fixture.target.environment))
        return environment

    def _assert_cleanup(self, fixture: RttFixture, output: str) -> None:
        session = SESSION_PATTERN.search(output)
        assert session is not None, output
        result = SshCommand(fixture.target.host.ssh_command).run(
            fixture.target.host.ssh_host,
            f"test ! -e {shlex.quote(session.group(2))}",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    @staticmethod
    def _west_command(fixture: RttFixture, command: str, *runner_args: str) -> list[str]:
        return [
            str(fixture.target.build_environment.west),
            command,
            "-d",
            str(fixture.target.build_dir),
            "-r",
            "remote_openocd",
            "--no-rebuild",
            "--",
            *fixture.target.runner_args,
            *map(str, runner_args),
        ]

    def _start(self, fixture: RttFixture, command: str, *runner_args: str):
        return subprocess.Popen(
            self._west_command(fixture, command, *runner_args),
            cwd=fixture.target.workspace,
            env=self._environment(fixture),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _program(self, fixture: RttFixture) -> None:
        result = subprocess.run(
            self._west_command(fixture, "flash"),
            cwd=fixture.target.workspace,
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout
        self._assert_cleanup(fixture, result.stdout)

    def _finish(self, fixture: RttFixture, process, output, *, interrupt=False):
        if process.poll() is None and interrupt:
            process.send_signal(signal.SIGINT)
        try:
            remainder, _ = process.communicate(timeout=20)
            output.extend(remainder)
        except subprocess.TimeoutExpired:
            process.kill()
            output.extend(process.communicate()[0])
            pytest.fail("RTT west process did not terminate")
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

    def _rtt_round_trip(self, fixture: RttFixture, port: int) -> None:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
            connection.settimeout(1)
            request = fixture.operation.input.encode()
            expected = fixture.operation.response.encode()
            received = bytearray()
            deadline = time.monotonic() + fixture.operation.timeout
            next_send = 0.0
            while expected not in received and time.monotonic() < deadline:
                if time.monotonic() >= next_send:
                    connection.sendall(request)
                    next_send = time.monotonic() + 1
                try:
                    received.extend(connection.recv(4096))
                except TimeoutError:
                    continue
            assert expected in received, received.decode("utf-8", "replace")

    def test_standalone_rtt(self, rtt_fixture: RttFixture) -> None:
        fixture = rtt_fixture
        self._program(fixture)
        port = fixture.operation.port
        process = self._start(fixture, "rtt", f"--rtt-port={port}")
        output = bytearray()
        try:
            read_until(process, RTT_ENDPOINT_PATTERN.pattern, 90, output)
            assert process.poll() is None
            assert f"127.0.0.1:{port}".encode() in output
            time.sleep(1)
            assert process.stdin is not None
            process.stdin.write(fixture.operation.input.encode())
            process.stdin.flush()
            read_until(
                process,
                fixture.operation.response,
                fixture.operation.timeout,
                output,
            )
        finally:
            text = self._finish(fixture, process, output, interrupt=True)
        assert SESSION_PATTERN.search(text)

    def test_debug_rtt_server_keeps_gdb_foreground(self, rtt_fixture: RttFixture) -> None:
        fixture = rtt_fixture
        breakpoint = fixture.operation.breakpoint
        port = fixture.operation.port
        process = self._start(
            fixture,
            "debug",
            "--rtt-server",
            f"--rtt-port={port}",
            f"--gdb-init=break {breakpoint}",
            "--gdb-init=continue",
            '--gdb-init=printf "ZRO_PC_BEGIN\\n"',
            "--gdb-init=p/x $pc",
            '--gdb-init=printf "ZRO_PC_END\\n"',
            '--gdb-init=printf "ZRO_INSN_BEGIN\\n"',
            "--gdb-init=x/1i $pc",
            '--gdb-init=printf "ZRO_INSN_END\\n"',
            "--gdb-init=delete breakpoints",
            "--gdb-init=monitor resume",
            "--gdb-init=echo ZRO_GDB_RTT_READY\\n",
            "--gdb-init=shell sleep 15",
            "--gdb-init=detach",
            "--gdb-init=quit",
        )
        output = bytearray()
        try:
            read_until(process, RTT_ENDPOINT_PATTERN.pattern, 90, output)
            read_until(process, "ZRO_GDB_RTT_READY", 90, output)
            assert process.poll() is None
            try:
                self._rtt_round_trip(fixture, port)
            except (AssertionError, OSError) as error:
                text = self._finish(fixture, process, output, interrupt=True)
                pytest.fail(f"{error}\n{text}")
            text = self._finish(fixture, process, output)
        finally:
            self._abort(process)
        assert re.search(rf"Breakpoint \d+,\s+{re.escape(breakpoint)}\b", text)
        assert re.search(r"ZRO_PC_BEGIN\s*\$\d+\s*=\s*0x[0-9a-fA-F]+", text)
        assert re.search(r"ZRO_INSN_BEGIN\s*=>?\s*0x[0-9a-fA-F]+", text)

    def test_debugserver_exposes_gdb_and_rtt_without_clients(self, rtt_fixture: RttFixture) -> None:
        fixture = rtt_fixture
        breakpoint = fixture.operation.breakpoint
        port = fixture.operation.port
        process = self._start(fixture, "debugserver", "--rtt-server", f"--rtt-port={port}")
        output = bytearray()
        try:
            read_until(process, RTT_ENDPOINT_PATTERN.pattern, 90, output)
            gdb_port = 3333
            client = subprocess.run(
                [
                    str(fixture.target.gdb),
                    "-q",
                    "-batch",
                    str(fixture.target.elf_file),
                    "-ex",
                    f"target extended-remote 127.0.0.1:{gdb_port}",
                    "-ex",
                    "load",
                    "-ex",
                    f"break {breakpoint}",
                    "-ex",
                    "continue",
                    "-ex",
                    'printf "ZRO_PC_BEGIN\\n"',
                    "-ex",
                    "p/x $pc",
                    "-ex",
                    'printf "ZRO_PC_END\\n"',
                    "-ex",
                    'printf "ZRO_INSN_BEGIN\\n"',
                    "-ex",
                    "x/1i $pc",
                    "-ex",
                    'printf "ZRO_INSN_END\\n"',
                    "-ex",
                    "delete breakpoints",
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
            assert re.search(rf"Breakpoint \d+,\s+{re.escape(breakpoint)}\b", client.stdout)
            assert re.search(r"ZRO_PC_BEGIN\s*\$\d+\s*=\s*0x[0-9a-fA-F]+", client.stdout)
            assert re.search(r"ZRO_INSN_BEGIN\s*=>?\s*0x[0-9a-fA-F]+", client.stdout)
            self._rtt_round_trip(fixture, port)
            self._finish(fixture, process, output, interrupt=True)
        finally:
            self._abort(process)
