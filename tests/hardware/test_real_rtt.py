# SPDX-License-Identifier: Apache-2.0

"""Fixture-gated destructive acceptance tests for real RTT transport."""

from __future__ import annotations

import shlex
import signal
import socket
import subprocess
import time

import pytest

from tests.hardware_support import RttFixture, free_loopback_ports, hardware_operation_environment
from tests.process_support import managed_process, run_process

WEST_SHUTDOWN_TIMEOUT = 20
GDB_CLIENT_TIMEOUT = 30
SERVER_READY_TIMEOUT = 90

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealRtt:
    """Validate channel-0 RTT and the two RTT server variants."""

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
        return managed_process(
            self._west_command(fixture, command, *runner_args),
            cwd=fixture.target.workspace,
            env=hardware_operation_environment(fixture.target),
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.STDOUT,
        )

    @staticmethod
    def _connect_endpoint(
        port: int, timeout: float, *, process: subprocess.Popen[bytes] | None = None
    ) -> socket.socket:
        deadline = time.monotonic() + timeout
        last_error: OSError | None = None
        while (remaining := deadline - time.monotonic()) > 0:
            if process is not None and process.poll() is not None:
                raise AssertionError("west process exited before endpoint readiness")
            try:
                return socket.create_connection(("127.0.0.1", port), timeout=min(1.0, remaining))
            except OSError as error:
                last_error = error
                time.sleep(min(0.1, remaining))
        message = f"endpoint 127.0.0.1:{port} did not become ready"
        raise AssertionError(message) from last_error

    def _program(self, fixture: RttFixture) -> None:
        result = run_process(
            self._west_command(fixture, "flash"),
            cwd=fixture.target.workspace,
            env=hardware_operation_environment(fixture.target),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout

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
        with self._connect_endpoint(port, fixture.operation.timeout) as connection:
            self._exchange_rtt(connection, fixture)

    def test_standalone_rtt(self, rtt_fixture: RttFixture) -> None:
        fixture = rtt_fixture
        self._program(fixture)
        port = fixture.operation.port
        with self._start(fixture, "rtt", f"--rtt-port={port}") as owner:
            process = owner.process
            output = owner.capture_output()
            assert process.poll() is None
            assert process.stdin is not None
            process.stdin.write(fixture.operation.input.encode())
            process.stdin.flush()
            output.wait_for(fixture.operation.response, fixture.operation.timeout)
            process.send_signal(signal.SIGINT)
            process.wait(timeout=WEST_SHUTDOWN_TIMEOUT)

    def test_debug_rtt_server_keeps_gdb_active(self, rtt_fixture: RttFixture, tmp_path) -> None:
        fixture = rtt_fixture
        port = fixture.operation.port
        release = tmp_path / "release-gdb"
        with self._start(
            fixture,
            "debug",
            "--rtt-server",
            f"--rtt-port={port}",
            "--gdb-init=monitor resume",
            "--gdb-init=echo ZRO_GDB_RTT_READY\\n",
            f"--gdb-init=shell while test ! -e {shlex.quote(str(release))}; do sleep 0.1; done",
            "--gdb-init=detach",
            "--gdb-init=quit",
        ) as owner:
            process = owner.process
            output = owner.capture_output()
            try:
                output.wait_for("ZRO_GDB_RTT_READY", timeout=SERVER_READY_TIMEOUT)
                assert process.poll() is None
                self._rtt_round_trip(fixture, port)
            finally:
                release.touch()
            process.wait(timeout=WEST_SHUTDOWN_TIMEOUT)
        assert process.returncode == 0, output.text

    def test_debugserver_serves_gdb_and_rtt_concurrently(
        self, rtt_fixture: RttFixture, tmp_path
    ) -> None:
        fixture = rtt_fixture
        port = fixture.operation.port
        gdb_client_port = free_loopback_ports(1)[0]
        release = tmp_path / "release-debugserver-gdb"
        with self._start(
            fixture,
            "debugserver",
            "--rtt-server",
            f"--rtt-port={port}",
            f"--gdb-client-port={gdb_client_port}",
        ) as server_owner:
            process = server_owner.process
            output = server_owner.capture_output()
            try:
                try:
                    with self._connect_endpoint(
                        gdb_client_port, timeout=SERVER_READY_TIMEOUT, process=process
                    ):
                        pass
                except AssertionError as error:
                    raise AssertionError(f"{error}\n{output.text}") from error
                assert process.poll() is None, output.text
                with managed_process(
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
                        "monitor resume",
                        "-ex",
                        "echo ZRO_GDB_RTT_READY\\n",
                        "-ex",
                        f"shell while test ! -e {shlex.quote(str(release))}; do sleep 0.1; done",
                        "-ex",
                        "detach",
                        "-ex",
                        "quit",
                    ],
                    env=hardware_operation_environment(fixture.target),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                ) as client_owner:
                    client = client_owner.process
                    client_output = client_owner.capture_output()
                    try:
                        client_output.wait_for("ZRO_GDB_RTT_READY", timeout=GDB_CLIENT_TIMEOUT)
                        assert client.poll() is None
                        self._rtt_round_trip(fixture, port)
                    finally:
                        release.touch()
                    client.wait(timeout=GDB_CLIENT_TIMEOUT)
                assert client.returncode == 0, f"{client_output.text}\n{output.text}"
                process.send_signal(signal.SIGINT)
                process.wait(timeout=WEST_SHUTDOWN_TIMEOUT)
            finally:
                release.touch()
