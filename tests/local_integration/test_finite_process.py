# SPDX-License-Identifier: Apache-2.0

"""Finite SSH commands through real subprocesses, pipes, and signals."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any

import pytest
from zephyr_remote_openocd.controller import _processes as processes
from zephyr_remote_openocd.controller._ssh import SshCommand
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.waiting import WaitResult

if TYPE_CHECKING:
    from _typeshed import TraceFunction

pytestmark = [pytest.mark.local, pytest.mark.skipif(os.name != "posix", reason="POSIX pipes")]

GUARD = 60.0
# Larger than any default Linux pipe buffer, in both directions.
PAYLOAD = bytes(range(256)) * 4096


def local_ssh(code: str, *arguments: str) -> SshCommand:
    """The configured SSH prefix is a local program; host/command are trailing args."""
    return SshCommand((sys.executable, "-c", code, *arguments))


def child_pids(marker: Path) -> list[int]:
    return [int(line) for line in marker.read_text(encoding="ascii").split()]


def assert_reaped(pid: int) -> None:
    with pytest.raises(ChildProcessError):
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG)


class FifoGate:
    """Deterministic handshakes with a child through named pipes."""

    def __init__(self, root: Path) -> None:
        self.entered = root / "entered"
        self.release = root / "release"
        os.mkfifo(self.entered)
        os.mkfifo(self.release)

    def wait_entered(self) -> bytes:
        with self.entered.open("rb", buffering=0) as stream:
            return stream.read(64)

    def open_release(self, payload: bytes) -> None:
        with self.release.open("wb", buffering=0) as stream:
            stream.write(payload)

    def releaser(self, payload: bytes) -> threading.Thread:
        def release_after_entry() -> None:
            self.wait_entered()
            self.open_release(payload)

        return threading.Thread(target=release_after_entry)


def test_large_input_and_outputs_are_drained_without_deadlock() -> None:
    code = (
        "import sys\n"
        "data = sys.stdin.buffer.read()\n"
        "sys.stderr.buffer.write(data[::-1]); sys.stderr.flush()\n"
        "sys.stdout.buffer.write(data)\n"
        "sys.exit(sys.argv[-1] == 'command' and 3)\n"
    )
    result = local_ssh(code).run("host", "command", input_data=PAYLOAD, timeout=GUARD)
    assert result.returncode == 3
    assert result.stdout == PAYLOAD
    assert result.stderr == PAYLOAD[::-1]
    assert result.args[-2:] == ["host", "command"]


def test_command_without_input_inherits_stdin() -> None:
    code = "import os; s = os.fstat(0); print(s.st_dev, s.st_ino)"
    parent = os.fstat(0)
    result = local_ssh(code).run("host", "command", timeout=GUARD)
    assert result.stdout.split() == [str(parent.st_dev).encode(), str(parent.st_ino).encode()]


def test_stream_payload_becomes_the_command_input() -> None:
    code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    with tempfile.TemporaryFile() as stream:
        stream.write(PAYLOAD)
        stream.seek(0)
        result = local_ssh(code).run_stream("host", "stage", stream, timeout=GUARD)
        assert not stream.closed
    assert result.stdout == PAYLOAD


def test_deadline_stops_and_reaps_the_command(tmp_path: Path) -> None:
    marker = tmp_path / "pids"
    code = (
        "import os, signal\n"
        f"open({str(marker)!r}, 'a').write(f'{{os.getpid()}}\\n')\n"
        "signal.pause()\n"
    )
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        # The command never reads its full input, so it can only end by deadline.
        local_ssh(code).run("host", "command", input_data=PAYLOAD, timeout=0.5)
    assert raised.value.timeout == 0.5
    (pid,) = child_pids(marker)
    assert_reaped(pid)


@pytest.mark.timeout(int(GUARD))
def test_cancellation_under_input_backpressure_stops_without_replay(tmp_path: Path) -> None:
    marker = tmp_path / "pids"
    gate = FifoGate(tmp_path)
    code = (
        "import os, signal\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        f"open({str(marker)!r}, 'a').write(f'{{os.getpid()}}\\n')\n"
        f"open({str(gate.entered)!r}, 'wb').close()\n"
        "signal.pause()\n"
    )
    opened = list(Path("/proc/self/fd").iterdir())

    def interrupt_when_blocked() -> None:
        gate.wait_entered()
        os.kill(os.getpid(), signal.SIGINT)

    sender = threading.Thread(target=interrupt_when_blocked)
    sender.start()
    try:
        with pytest.raises(OperationCancelled), operation_scope():
            local_ssh(code).run("host", "command", input_data=PAYLOAD, timeout=GUARD)
            pytest.fail("cancellation did not interrupt the blocked command")
    finally:
        sender.join(GUARD)
    (pid,) = child_pids(marker)
    assert_reaped(pid)
    assert sorted(Path("/proc/self/fd").iterdir()) == sorted(opened)


def test_admission_refuses_a_command_after_cancellation(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    code = f"open({str(marker)!r}, 'w').close()"
    with pytest.raises(OperationCancelled), operation_scope():
        signal.raise_signal(signal.SIGINT)
        local_ssh(code).run("host", "command", timeout=GUARD)
    assert not marker.exists()


@pytest.mark.timeout(int(GUARD))
def test_process_exit_is_observed_separately_from_output_eof(tmp_path: Path) -> None:
    gate = FifoGate(tmp_path)
    code = (
        "import os, sys\n"
        "os.close(1); os.close(2)\n"
        f"open({str(gate.entered)!r}, 'wb').close()\n"
        f"sys.exit(open({str(gate.release)!r}, 'rb').read(1)[0])\n"
    )
    releaser = gate.releaser(b"\x07")
    releaser.start()
    try:
        # EOF arrives first; the status exists only after the gated exit.
        result = local_ssh(code).run("host", "command", timeout=GUARD)
    finally:
        releaser.join(GUARD)
    assert result.returncode == 7


@pytest.mark.timeout(int(GUARD))
def test_output_eof_is_awaited_after_process_exit(tmp_path: Path) -> None:
    gate = FifoGate(tmp_path)
    descendant = (
        "import os, sys\n"
        f"open({str(gate.entered)!r}, 'wb').close()\n"
        f"open({str(gate.release)!r}, 'rb').read(1)\n"
        "sys.stdout.write('late output')\n"
    )
    code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
        "print('early', flush=True)\n"
    )
    releaser = gate.releaser(b"x")
    releaser.start()
    try:
        result = local_ssh(code).run("host", "command", timeout=GUARD)
    finally:
        releaser.join(GUARD)
    assert result.returncode == 0
    assert result.stdout == b"early\nlate output"


def test_interrupted_wait_settles_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "pids"
    code = (
        "import os, sys\n"
        f"open({str(marker)!r}, 'a').write(f'{{os.getpid()}}\\n')\n"
        "sys.stdin.buffer.read()\n"
    )
    failure = KeyboardInterrupt()
    real_wait = processes.wait_for

    def interrupted_wait(**kwargs: Any) -> WaitResult:
        # The child records itself before it drains the first full pipe.
        if marker.exists():
            raise failure
        return real_wait(**kwargs)

    opened = list(Path("/proc/self/fd").iterdir())
    monkeypatch.setattr(processes, "wait_for", interrupted_wait)
    with pytest.raises(KeyboardInterrupt) as raised:
        local_ssh(code).run("host", "command", input_data=PAYLOAD, timeout=GUARD)
    assert raised.value is failure
    (pid,) = child_pids(marker)
    assert_reaped(pid)
    assert sorted(Path("/proc/self/fd").iterdir()) == sorted(opened)


@pytest.mark.parametrize("failure_type", (MemoryError, SystemExit))
def test_failure_at_popen_return_reaps_the_finite_command(
    failure_type: type[BaseException],
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    failure = failure_type("finite command constructor return failed")
    previous_trace = sys.gettrace()

    def fail_at_return(frame: FrameType, event: str, _arg: object) -> TraceFunction | None:
        # Arbitrary BaseException after CPython published the child, before the
        # caller's assignment from the constructor could adopt it.
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            process = frame.f_locals["self"]
            assert process.pid is not None
            processes.append(process)
            raise failure
        return fail_at_return

    code = "import sys; sys.stdin.buffer.read()"
    try:
        sys.settrace(fail_at_return)
        try:
            with pytest.raises(failure_type) as raised:
                local_ssh(code).run("host", "command", input_data=PAYLOAD, timeout=GUARD)
        finally:
            sys.settrace(previous_trace)
        assert raised.value is failure
        assert len(processes) == 1
        process = processes[0]
        # Production cleanup terminated, reaped, and closed every pipe.
        assert_reaped(process.pid)
        assert all(
            stream is None or stream.closed
            for stream in (process.stdin, process.stdout, process.stderr)
        )
    finally:
        sys.settrace(previous_trace)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(GUARD)
