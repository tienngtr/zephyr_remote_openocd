# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile
from zephyr_remote_openocd.controller._ssh import SshCommand

from tests.acceptance.firmware import establish_precondition
from tests.hardware_support import (
    AttachFixture,
    DebugFixture,
    DebugServerFixture,
    ThreadInfoFixture,
    free_loopback_ports,
    hardware_operation_environment,
)
from tests.process_support import cleanup_on_exit, managed_process, run_process
from tests.serial_reader import captured_text, endpoint_reader_command, read_event, stop_reader

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]

_CLIENT_TIMEOUT = 30

DebugHardwareFixture = DebugFixture | AttachFixture | DebugServerFixture | ThreadInfoFixture


class DebugAcceptance:
    def _west_command(
        self,
        fixture: DebugHardwareFixture,
        command: str,
        build_dir=None,
        extra_args=(),
    ) -> list[str]:
        target = fixture.target
        result = [
            str(target.build_environment.west),
            command,
            "-d",
            str(build_dir or target.build_dir),
            "-r",
            "remote_openocd",
            "--no-rebuild",
        ]
        runner_args = [*target.runner_args, *extra_args]
        if runner_args:
            result.extend(("--", *map(str, runner_args)))
        return result

    def debug(self, fixture: DebugFixture) -> None:
        witness = fixture.precondition.memory_witness(fixture.target)
        establish_precondition(fixture.target, fixture.precondition)
        breakpoint = fixture.operation.breakpoint
        commands = (
            # The runner loads before --gdb-init, so this reads what load wrote.
            *witness.gdb_commands(),
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
        result = run_process(
            command,
            cwd=fixture.target.workspace,
            env=hardware_operation_environment(fixture.target),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout
        assert witness.observed(result.stdout) == witness.selected, result.stdout
        assert re.search(rf"Breakpoint \d+,\s+{re.escape(breakpoint)}\b", result.stdout)
        assert re.search(r"ZRO_PC_BEGIN\s*\$\d+\s*=\s*0x[0-9a-fA-F]+", result.stdout)
        assert re.search(r"ZRO_INSN_BEGIN\s*=>\s*0x[0-9a-fA-F]+", result.stdout)
        assert "ZRO_PC_END" in result.stdout
        assert "ZRO_INSN_END" in result.stdout
        for pattern in fixture.operation.output_patterns:
            assert re.search(pattern, result.stdout)

    def attach(self, fixture: AttachFixture) -> None:
        witness = fixture.precondition.memory_witness(fixture.target)
        establish_precondition(fixture.target, fixture.precondition)
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
                *(f"--gdb-init={item}" for item in witness.gdb_commands()),
                "--gdb-init=detach",
                "--gdb-init=quit",
            ),
        )
        result = run_process(
            command,
            cwd=fixture.target.workspace,
            env=hardware_operation_environment(fixture.target),
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
        assert witness.observed(result.stdout) == witness.precondition, result.stdout

    def debugserver(self, fixture: DebugServerFixture, output_path: Path) -> None:
        # A quiet image ensures the pattern can only come from the GDB session.
        establish_precondition(fixture.target, fixture.precondition)
        gdb_port, tcl_port, telnet_port = free_loopback_ports(3)
        extra_args = (
            f"--gdb-port={gdb_port}",
            f"--tcl-port={tcl_port}",
            f"--telnet-port={telnet_port}",
        )
        with (
            output_path.open("w", encoding="utf-8") as output,
            managed_process(
                self._west_command(fixture, "debugserver", extra_args=extra_args),
                cwd=fixture.target.workspace,
                env=hardware_operation_environment(fixture.target),
                text=True,
                stdout=output,
                stderr=subprocess.STDOUT,
            ) as owner,
        ):
            process = owner.process

            def diagnostics() -> str:
                output.flush()
                return output_path.read_text(encoding="utf-8", errors="replace")

            pending_ports = {tcl_port, telnet_port}
            end = time.monotonic() + 90
            while pending_ports and time.monotonic() < end:
                if process.poll() is not None:
                    pytest.fail("debugserver exited before readiness:\n" + diagnostics())
                for port in tuple(pending_ports):
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            pending_ports.remove(port)
                    except OSError:
                        pass
                if pending_ports:
                    time.sleep(0.1)
            if pending_ports:
                pytest.fail(
                    "debugserver endpoints did not become ready "
                    f"({sorted(pending_ports)}):\n" + diagnostics()
                )
            assert process.poll() is None, (
                "debugserver exited before client connection:\n" + diagnostics()
            )
            self._debugserver_controls_observed_board(fixture, gdb_port)
            assert process.poll() is None, (
                "debugserver did not remain persistent:\n" + diagnostics()
            )
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)

    def _debugserver_controls_observed_board(
        self, fixture: DebugServerFixture, gdb_port: int
    ) -> None:
        """Require output on this target's serial port caused by the GDB session."""
        observation = fixture.operation.serial
        host = fixture.target.host
        reader = SshCommand(host.ssh_command).popen(
            host.ssh_host,
            # The reader deadline starts when armed and also covers the GDB client.
            endpoint_reader_command(
                fixture.serial, observation.pattern, observation.timeout + _CLIENT_TIMEOUT
            ),
        )
        with cleanup_on_exit(lambda: stop_reader(reader)):
            assert read_event(reader, 15)["type"] == "READY"
            assert reader.stdin is not None
            # Arm after debugserver startup so only output caused by the client counts.
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()
            assert read_event(reader, 15)["type"] == "ARMED"
            with fixture.target.elf_file.open("rb") as stream:
                entry = ELFFile(stream).header["e_entry"]
            # Start the image as Zephyr's ELF flash does: a reset alone may boot
            # unrelated persistent firmware instead.
            client = self._debugserver_client(
                fixture,
                gdb_port,
                ("monitor reset init", "load", f"monitor resume 0x{entry:x}"),
            )
            try:
                event = read_event(reader, observation.timeout + _CLIENT_TIMEOUT + 2)
            except AssertionError:
                pytest.fail(
                    f"serial reader failed after debugserver client:\n{client.stdout}",
                    pytrace=False,
                )
            captured = captured_text(event)
            assert client.returncode == 0, client.stdout + "\nserial:\n" + captured
            assert event["type"] == "MATCH", f"serial oracle failed: {event}\n{captured}"
            assert reader.wait(timeout=5) == 0

    @staticmethod
    def _debugserver_client(
        fixture: DebugServerFixture, gdb_port: int, commands: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        """Run an independent GDB client that connects, then runs ``commands``."""
        arguments = [
            str(fixture.target.gdb),
            "-q",
            "-batch",
            str(fixture.target.elf_file),
        ]
        for command in (
            f"target extended-remote 127.0.0.1:{gdb_port}",
            *commands,
            "detach",
            "quit",
        ):
            arguments.extend(("-ex", command))
        return run_process(
            arguments,
            env=hardware_operation_environment(fixture.target),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=_CLIENT_TIMEOUT,
        )

    def thread_info(self, thread_info_fixture: ThreadInfoFixture) -> None:
        fixture = thread_info_fixture
        prepare = self._west_command(
            fixture,
            "flash",
            fixture.target.build_dir,
        )
        prepared = run_process(
            prepare,
            cwd=fixture.target.workspace,
            env=hardware_operation_environment(fixture.target),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert prepared.returncode == 0, prepared.stdout
        command = self._west_command(
            fixture,
            "attach",
            fixture.target.build_dir,
            ("--gdb-init=info threads", "--gdb-init=detach", "--gdb-init=quit"),
        )
        result = run_process(
            command,
            cwd=fixture.target.workspace,
            env=hardware_operation_environment(fixture.target),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout
        assert re.search(fixture.operation.pattern, result.stdout)
