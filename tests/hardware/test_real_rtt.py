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

from tests.hardware_support import RttFixture, free_loopback_ports
from tests.process_support import read_until
from tests.support import ROOT

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


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

    def _start(
        self,
        fixture: RttFixture,
        command: str,
        *runner_args: str,
        stdout=subprocess.PIPE,
    ):
        return subprocess.Popen(
            self._west_command(fixture, command, *runner_args),
            cwd=fixture.target.workspace,
            env=self._environment(fixture),
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.STDOUT,
        )

    @staticmethod
    def _connect_rtt(port: int, timeout: float) -> socket.socket:
        deadline = time.monotonic() + timeout
        last_error: OSError | None = None
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                return socket.create_connection(("127.0.0.1", port), timeout=min(1.0, remaining))
            except OSError as error:
                last_error = error
                time.sleep(min(0.1, remaining))
        message = f"RTT endpoint 127.0.0.1:{port} did not become ready"
        raise AssertionError(message) from last_error

    @staticmethod
    def _wait_for_gdb(fixture: RttFixture, port: int, timeout: float, process, diagnostics) -> None:
        command = [
            str(fixture.target.gdb),
            "-q",
            "-batch",
            str(fixture.target.elf_file),
            "-ex",
            f"target extended-remote 127.0.0.1:{port}",
            "-ex",
            "detach",
            "-ex",
            "quit",
        ]
        deadline = time.monotonic() + timeout
        last_output = ""
        while (remaining := deadline - time.monotonic()) > 0:
            if process.poll() is not None:
                raise AssertionError(
                    f"west exited before GDB endpoint 127.0.0.1:{port} became ready:\n"
                    + diagnostics()
                )
            try:
                result = subprocess.run(
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=min(10, remaining),
                )
            except subprocess.TimeoutExpired as error:
                last_output = str(error)
            else:
                if result.returncode == 0:
                    return
                last_output = result.stdout
            time.sleep(min(0.1, remaining))
        raise AssertionError(
            f"GDB endpoint 127.0.0.1:{port} did not become ready:\n{last_output}\n{diagnostics()}"
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

    def _finish(self, fixture: RttFixture, process, output, *, interrupt=False):
        if process.poll() is None and interrupt:
            process.send_signal(signal.SIGINT)
        try:
            remainder, _ = process.communicate(timeout=20)
            if remainder:
                output.extend(remainder)
        except subprocess.TimeoutExpired:
            process.kill()
            remainder = process.communicate()[0]
            if remainder:
                output.extend(remainder)
            pytest.fail("RTT west process did not terminate")
        text = bytes(output).decode("utf-8", "replace")
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

    @staticmethod
    def _exchange_rtt(connection: socket.socket, fixture: RttFixture) -> None:
        deadline = time.monotonic() + fixture.operation.timeout
        connection.settimeout(1)
        request = fixture.operation.input.encode()
        expected = fixture.operation.response.encode()
        received = bytearray()
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

    def _rtt_round_trip(self, fixture: RttFixture, port: int) -> None:
        with self._connect_rtt(port, fixture.operation.timeout) as connection:
            self._exchange_rtt(connection, fixture)

    def test_standalone_rtt(self, rtt_fixture: RttFixture) -> None:
        fixture = rtt_fixture
        self._program(fixture)
        port = fixture.operation.port
        process = self._start(fixture, "rtt", f"--rtt-port={port}")
        output = bytearray()
        try:
            assert process.poll() is None
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
            self._finish(fixture, process, output, interrupt=True)

    def test_debug_rtt_server_keeps_gdb_foreground(self, rtt_fixture: RttFixture, tmp_path) -> None:
        fixture = rtt_fixture
        breakpoint = fixture.operation.breakpoint
        port = fixture.operation.port
        release = tmp_path / "release-gdb"
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
            f"--gdb-init=shell while test ! -e {shlex.quote(str(release))}; do sleep 0.1; done",
            "--gdb-init=detach",
            "--gdb-init=quit",
        )
        output = bytearray()
        try:
            read_until(process, "ZRO_GDB_RTT_READY", timeout=90, output=output)
            assert process.poll() is None
            try:
                self._rtt_round_trip(fixture, port)
            except (AssertionError, OSError) as error:
                release.touch()
                text = self._finish(fixture, process, output)
                pytest.fail(f"{error}\n{text}")
            release.touch()
            text = self._finish(fixture, process, output)
        finally:
            release.touch()
            self._abort(process)
        assert re.search(rf"Breakpoint \d+,\s+{re.escape(breakpoint)}\b", text)
        assert re.search(r"ZRO_PC_BEGIN\s*\$\d+\s*=\s*0x[0-9a-fA-F]+", text)
        assert re.search(r"ZRO_INSN_BEGIN\s*=>?\s*0x[0-9a-fA-F]+", text)

    def test_debugserver_exposes_gdb_and_rtt_without_clients(
        self, rtt_fixture: RttFixture, tmp_path
    ) -> None:
        fixture = rtt_fixture
        breakpoint = fixture.operation.breakpoint
        port = fixture.operation.port
        gdb_client_port = free_loopback_ports(1)[0]
        output_path = tmp_path / "debugserver-rtt.log"
        with output_path.open("wb") as log:
            process = self._start(
                fixture,
                "debugserver",
                "--rtt-server",
                f"--rtt-port={port}",
                f"--gdb-client-port={gdb_client_port}",
                stdout=log,
            )
            output = bytearray()

            def diagnostics() -> str:
                log.flush()
                return output_path.read_bytes().decode("utf-8", "replace")

            try:
                self._wait_for_gdb(fixture, gdb_client_port, 90, process, diagnostics)
                client = subprocess.run(
                    [
                        str(fixture.target.gdb),
                        "-q",
                        "-batch",
                        str(fixture.target.elf_file),
                        "-ex",
                        f"target extended-remote 127.0.0.1:{gdb_client_port}",
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
                assert client.returncode == 0, f"{client.stdout}\n{diagnostics()}"
                assert re.search(rf"Breakpoint \d+,\s+{re.escape(breakpoint)}\b", client.stdout), (
                    f"{client.stdout}\n{diagnostics()}"
                )
                assert re.search(r"ZRO_PC_BEGIN\s*\$\d+\s*=\s*0x[0-9a-fA-F]+", client.stdout), (
                    f"{client.stdout}\n{diagnostics()}"
                )
                assert re.search(r"ZRO_INSN_BEGIN\s*=>?\s*0x[0-9a-fA-F]+", client.stdout), (
                    f"{client.stdout}\n{diagnostics()}"
                )
                try:
                    self._rtt_round_trip(fixture, port)
                except (AssertionError, OSError) as error:
                    pytest.fail(f"{error}\n{diagnostics()}", pytrace=False)
                self._finish(fixture, process, output, interrupt=True)
            finally:
                self._abort(process)
