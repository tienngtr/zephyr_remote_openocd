# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from tests.hardware_support import (
    AttachFixture,
    DebugFixture,
    DebugServerFixture,
    ThreadInfoFixture,
    elf_memory_witness,
    free_loopback_ports,
    hardware_operation_environment,
)
from tests.process_support import managed_process, run_process

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]

DebugHardwareFixture = DebugFixture | AttachFixture | DebugServerFixture | ThreadInfoFixture


class TestRealOpenOcdDebug:
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

    def test_debug(self, debug_fixture: DebugFixture) -> None:
        self._debug(debug_fixture)

    def test_attach(self, attach_fixture: AttachFixture) -> None:
        self._attach(attach_fixture)

    def test_debugserver(self, debugserver_fixture: DebugServerFixture, tmp_path: Path) -> None:
        self._debugserver(debugserver_fixture, tmp_path / "debugserver.log")

    def _debug(self, fixture: DebugFixture) -> None:
        breakpoint = fixture.operation.breakpoint
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
        assert re.search(rf"Breakpoint \d+,\s+{re.escape(breakpoint)}\b", result.stdout)
        assert re.search(r"ZRO_PC_BEGIN\s*\$\d+\s*=\s*0x[0-9a-fA-F]+", result.stdout)
        assert re.search(r"ZRO_INSN_BEGIN\s*=>\s*0x[0-9a-fA-F]+", result.stdout)
        assert "ZRO_PC_END" in result.stdout
        assert "ZRO_INSN_END" in result.stdout
        for pattern in fixture.operation.output_patterns:
            assert re.search(pattern, result.stdout)

    def _attach(self, fixture: AttachFixture) -> None:
        prepared = run_process(
            self._west_command(fixture, "flash", fixture.precondition_build_dir),
            cwd=fixture.target.workspace,
            env=hardware_operation_environment(fixture.target),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert prepared.returncode == 0, prepared.stdout
        address, precondition_bytes, selected_bytes = elf_memory_witness(
            fixture.precondition_elf_file, fixture.target.elf_file
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

    def _debugserver(self, fixture: DebugServerFixture, output_path: Path) -> None:
        gdb_client_port, tcl_port, telnet_port = free_loopback_ports(3)
        extra_args = (
            f"--gdb-client-port={gdb_client_port}",
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
            client = run_process(
                [
                    str(fixture.target.gdb),
                    "-q",
                    "-batch",
                    str(fixture.target.elf_file),
                    "-ex",
                    f"target extended-remote 127.0.0.1:{gdb_client_port}",
                    "-ex",
                    "monitor halt",
                    "-ex",
                    "monitor resume",
                    "-ex",
                    "detach",
                    "-ex",
                    "quit",
                ],
                env=hardware_operation_environment(fixture.target),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=30,
            )
            assert client.returncode == 0, client.stdout
            assert process.poll() is None, (
                "debugserver did not remain persistent:\n" + diagnostics()
            )
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)

    def test_thread_info_on_capable_fixture(self, thread_info_fixture: ThreadInfoFixture) -> None:
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
