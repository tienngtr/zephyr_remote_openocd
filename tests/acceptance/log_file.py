# SPDX-License-Identifier: Apache-2.0

"""Hardware acceptance for ``--log-file`` with a stalled local consumer.

The local ``west`` process is stopped, so it reads neither OpenOCD output nor
log data. Its SSH transports keep running, so the remote helper's protocol
output backs up while OpenOCD continues. A log flood larger than the helper's
relay buffer must then overflow without stopping OpenOCD or the live server,
and the local file must remain an exact prefix of the channel stream.
"""

from __future__ import annotations

import re
import shlex
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from zephyr_remote_openocd._zephyr import adapter_module
from zephyr_remote_openocd.controller._ssh import SshCommand

from tests.acceptance.firmware import establish_precondition
from tests.hardware_support import (
    DebugServerFixture,
    FlashFixture,
    PreparedTarget,
    SemihostingFixture,
    free_loopback_ports,
    hardware_operation_environment,
)
from tests.process_support import cleanup_on_exit, managed_process, run_process
from tests.serial_reader import captured_text, endpoint_reader_command, read_event, stop_reader

# Each record is about 190 bytes; the flood is well beyond the 8 MiB relay
# buffer plus whatever the transport absorbs while the consumer is stopped.
FLOOD_RECORDS = 120_000
_FLOOD_RECORD = re.compile(r"ZRO_FLOOD (\d+) x{160}$")
_TCL_TERMINATOR = b"\x1a"


def flood(token: str) -> str:
    """Tcl that floods OpenOCD's log; ``token`` identifies the running process."""
    return (
        f"set zro_flood_token {token}; "
        f"for {{set zro_i 0}} {{$zro_i < {FLOOD_RECORDS}}} {{incr zro_i}} "
        '{echo "ZRO_FLOOD $zro_i [string repeat x 160]"}'
    )


def supports_log_file(target: PreparedTarget, tmp_path: Path) -> bool:
    """``--log-file`` exists only on the Zephyr 4.5 runner.

    On Zephyr 4.4, require the runner to reject the option before any
    effect, including on the local file, instead of skipping the node.
    """
    module = adapter_module(target.build_environment.zephyr_base / "VERSION")
    if module.endswith(".zephyr45.runner"):
        return True
    local_log = tmp_path / "unsupported openocd.log"
    result = run_process(
        _west(target, "flash", f"--log-file={local_log}"),
        cwd=target.workspace,
        env=hardware_operation_environment(target),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=120,
    )
    assert result.returncode != 0 and "--log-file" in result.stdout, result.stdout
    assert not local_log.exists()
    return False


def assert_flood_prefix(log: bytes) -> int:
    """Require flood records 0..n-1 in order, with no gap; return n."""
    numbers = [
        int(match.group(1))
        for line in log.decode("utf-8", "replace").splitlines()
        if (match := _FLOOD_RECORD.search(line))
    ]
    assert numbers == list(range(len(numbers))), "log file is not a contiguous prefix"
    return len(numbers)


def _west(target: PreparedTarget, command: str, *extra_args: str) -> list[str]:
    arguments = [str(target.build_environment.west), command]
    arguments.extend(("-d", str(target.build_dir), "-r", "remote_openocd", "--no-rebuild"))
    return [*arguments, "--", *target.runner_args, *extra_args]


def _await_remote_openocd(
    target: PreparedTarget, token: str, process: subprocess.Popen[str], output: Path
) -> None:
    """Poll the remote host until OpenOCD runs with ``token`` in its argv."""
    ssh = SshCommand(target.host.ssh_command)
    # The bracket keeps pgrep from matching its own command line.
    pattern = shlex.quote(f"[{token[0]}]{token[1:]}")
    end = time.monotonic() + 120
    while ssh.run(target.host.ssh_host, f"pgrep -f {pattern}", timeout=20).returncode:
        if process.poll() is not None or time.monotonic() > end:
            pytest.fail("remote OpenOCD did not start:\n" + output.read_text(errors="replace"))
        time.sleep(0.5)


def _await_endpoint(port: int, process: subprocess.Popen[str], output: Path) -> None:
    end = time.monotonic() + 120
    while True:
        if process.poll() is not None or time.monotonic() > end:
            pytest.fail("live server endpoint did not open:\n" + output.read_text(errors="replace"))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)


@contextmanager
def _stopped(process: subprocess.Popen[str]) -> Iterator[None]:
    """Stop only the local consumer; its SSH children keep their transports."""
    process.send_signal(signal.SIGSTOP)
    try:
        yield
    finally:
        process.send_signal(signal.SIGCONT)


def _tcl(port: int, command: str, timeout: float) -> str:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.sendall(command.encode() + _TCL_TERMINATOR)
        response = bytearray()
        while not response.endswith(_TCL_TERMINATOR):
            chunk = connection.recv(65536)
            assert chunk, "OpenOCD closed the Tcl connection"
            response.extend(chunk)
    return response[:-1].decode("utf-8", "replace")


class LogFileAcceptance:
    def stalled_flash(self, fixture: FlashFixture, tmp_path: Path) -> None:
        target = fixture.target
        if not supports_log_file(target, tmp_path):
            return
        establish_precondition(target, fixture.precondition)
        local_log = tmp_path / "flash openocd.log"
        output_path = tmp_path / "west.out"
        observation = fixture.operation.serial
        host = target.host
        reader = SshCommand(host.ssh_command).popen(
            host.ssh_host,
            endpoint_reader_command(fixture.serial, observation.pattern, observation.timeout + 300),
        )
        with cleanup_on_exit(lambda: stop_reader(reader)):
            assert read_event(reader, 15)["type"] == "READY"
            assert reader.stdin is not None
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()
            assert read_event(reader, 15)["type"] == "ARMED"
            token = f"ZRO_FLOOD_TOKEN_{uuid.uuid4().hex}"
            command = _west(
                target, "flash", f"--log-file={local_log}", f"--cmd-pre-init={flood(token)}"
            )
            with (
                output_path.open("w", encoding="utf-8") as output,
                managed_process(
                    command,
                    cwd=target.workspace,
                    env=hardware_operation_environment(target),
                    text=True,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                ) as owner,
            ):
                process = owner.process
                _await_remote_openocd(target, token, process, output_path)
                with _stopped(process):
                    # OpenOCD floods its log, then flashes and starts the
                    # image, while the local consumer reads nothing.
                    event = read_event(reader, observation.timeout + 300)
                returncode = process.wait(timeout=180)
            text = output_path.read_text(encoding="utf-8", errors="replace")
            assert event["type"] == "MATCH", f"{event}\n{captured_text(event)}\n{text}"
            # Flash succeeded on the target; only the incomplete log fails it.
            assert returncode != 0, text
            assert "LOG_RELAY_OVERFLOW" in text, text
            assert reader.wait(timeout=5) == 0
        assert 0 < assert_flood_prefix(local_log.read_bytes()) < FLOOD_RECORDS

    def stalled_debugserver(self, fixture: DebugServerFixture, tmp_path: Path) -> None:
        target = fixture.target
        if not supports_log_file(target, tmp_path):
            return
        establish_precondition(target, fixture.precondition)
        gdb_port, tcl_port, telnet_port = free_loopback_ports(3)
        local_log = tmp_path / "debugserver openocd.log"
        output_path = tmp_path / "west.out"
        command = _west(
            target,
            "debugserver",
            f"--gdb-port={gdb_port}",
            f"--tcl-port={tcl_port}",
            f"--telnet-port={telnet_port}",
            f"--log-file={local_log}",
        )
        with (
            output_path.open("w", encoding="utf-8") as output,
            managed_process(
                command,
                cwd=target.workspace,
                env=hardware_operation_environment(target),
                text=True,
                stdout=output,
                stderr=subprocess.STDOUT,
            ) as owner,
        ):
            process = owner.process
            _await_endpoint(tcl_port, process, output_path)
            with _stopped(process):
                # The Tcl and GDB forwards are separate SSH processes, so the
                # live server stays reachable while the consumer is stopped.
                _tcl(tcl_port, flood("debugserver"), 300)
                assert _tcl(tcl_port, "version", 30)
                client = run_process(
                    [
                        str(target.gdb),
                        "-q",
                        "-batch",
                        "-ex",
                        f"target extended-remote 127.0.0.1:{gdb_port}",
                        "-ex",
                        "monitor targets",
                        "-ex",
                        "detach",
                    ],
                    env=hardware_operation_environment(target),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=60,
                )
                assert client.returncode == 0, client.stdout
            assert process.poll() is None, "overflow stopped the live server"
            process.send_signal(signal.SIGINT)
            process.wait(timeout=60)
        text = output_path.read_text(encoding="utf-8", errors="replace")
        assert "LOG_RELAY_OVERFLOW" in text, text
        assert 0 < assert_flood_prefix(local_log.read_bytes()) < FLOOD_RECORDS

    def semihosting_stays_on_stdout(self, fixture: SemihostingFixture, tmp_path: Path) -> None:
        target = fixture.target
        if not supports_log_file(target, tmp_path):
            return
        local_log = tmp_path / "semihosting openocd.log"
        command = [
            str(target.build_environment.west),
            "debug",
            "-d",
            str(target.build_dir),
            "-r",
            "remote_openocd",
            "--no-rebuild",
            "--",
            *target.runner_args,
            *(f"--cmd-pre-init={item}" for item in fixture.operation.commands),
            *(f"--gdb-init={item}" for item in fixture.operation.gdb_commands),
            f"--log-file={local_log}",
        ]
        with managed_process(
            command,
            cwd=target.workspace,
            env=hardware_operation_environment(target),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as owner:
            stdout, stderr = owner.process.communicate(timeout=fixture.operation.timeout)
        text = stdout.decode("utf-8", "replace")
        diagnostics = text + stderr.decode("utf-8", "replace")
        assert owner.process.returncode == 0, diagnostics
        # Console output is written outside OpenOCD's logger.
        assert re.search(fixture.operation.output, text), diagnostics
        log = local_log.read_text(encoding="utf-8", errors="replace")
        assert log, "OpenOCD logger records did not reach the log file"
        assert not re.search(fixture.operation.output, log), log
