# SPDX-License-Identifier: Apache-2.0

"""Hardware cleanup owns descendants even after the command leader exits."""

from __future__ import annotations

import ctypes
import os
import select
import signal
import subprocess
import sys
import threading
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from queue import Queue
from typing import Any, NoReturn

import pytest

from tests.acceptance.rtt import RttAcceptance
from tests.acceptance.semihosting import SemihostingAcceptance
from tests.forwarding_support import RTT
from tests.hardware_support import PreparedTarget, RttFixture, SemihostingFixture
from tests.inventory import BuildEnvironment, InventoryHost, RttOperation, SemihostingOperation
from tests.process_support import managed_process, read_line
from tests.support import ROOT

# Linux UAPI constants from linux/prctl.h.
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
SYNC_TIMEOUT = 30
TEST_BACKSTOP_TIMEOUT = 60

_CHILD_TREE = ROOT / "tests/fixtures/ignoring_descendant_tree.py"


def _ignored_signals(pid: int) -> set[int]:
    """Read a live process's ignored-signal set from procfs."""
    for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
        if line.startswith("SigIgn:"):
            mask = int(line.split()[1], 16)
            return {
                signum for signum in range(1, mask.bit_length() + 1) if mask >> (signum - 1) & 1
            }
    raise AssertionError(f"process {pid} reports no ignored-signal set")


@pytest.mark.parametrize("operation", ("semihosting", "rtt-abort", "blocking-flash"))
@pytest.mark.timeout(TEST_BACKSTOP_TIMEOUT)
def test_hardware_cleanup_stops_descendant_after_leader_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    target = PreparedTarget(
        "target:profile",
        "target",
        "profile",
        InventoryHost("host", "unused", ("openocd",), ("ssh",), (), ()),
        BuildEnvironment("build", tmp_path, Path("west"), ()),
        None,
        tmp_path,
        tmp_path / "config.yaml",
        (),
        (),
    )
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    original_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []
    child_pid = None
    child_pidfd = None
    timeout = subprocess.TimeoutExpired("hardware-command", SYNC_TIMEOUT)

    # Popen's keyword arguments pass through; the command is replaced.
    def popen(*args: object, **kwargs: Any) -> subprocess.Popen[bytes]:
        nonlocal child_pid, child_pidfd
        assert kwargs.get("start_new_session") is True
        process = original_popen([sys.executable, str(_CHILD_TREE)], **kwargs)
        processes.append(process)
        assert process.stdout is not None
        child_pid = int(read_line(process.stdout, timeout=SYNC_TIMEOUT))
        child_pidfd = os.pidfd_open(child_pid)
        # The descendant installs its dispositions before the leader reports its
        # PID. Only the group SIGKILL can stop it, not graceful termination.
        assert {signal.SIGINT, signal.SIGTERM} <= _ignored_signals(child_pid)
        signal.pidfd_send_signal(child_pidfd, signal.SIGTERM)
        assert process.wait(timeout=SYNC_TIMEOUT) == 0

        def communicate(*args: object, **kwargs: object) -> NoReturn:
            raise timeout

        if operation != "rtt-abort":
            monkeypatch.setattr(process, "communicate", communicate)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    try:
        with pytest.raises(
            AssertionError if operation == "rtt-abort" else subprocess.TimeoutExpired
        ) as raised:
            if operation == "semihosting":
                SemihostingAcceptance().run(
                    SemihostingFixture(target, SemihostingOperation((), (), "ready", SYNC_TIMEOUT))
                )
            else:
                fixture = RttFixture(
                    target, RttOperation(RTT.local_port, "pong", "ping", SYNC_TIMEOUT, True)
                )
                if operation == "rtt-abort":
                    RttAcceptance().gdb_server(fixture, tmp_path, "debug")
                else:
                    RttAcceptance().program(fixture)
        if operation != "rtt-abort":
            assert raised.value is timeout
        assert child_pid is not None and child_pidfd is not None
        poller = select.poll()
        poller.register(child_pidfd, select.POLLIN)
        assert poller.poll(SYNC_TIMEOUT * 1000), "harness left its descendant alive"
        descendant = child_pid
        reaped, status = os.waitpid(descendant, 0)
        child_pid = None
        assert reaped == descendant
        # The wait status records the signal that ended it: SIGTERM did not.
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        assert processes[0].stdout is not None and processes[0].stdout.closed
    finally:
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
            os.waitpid(child_pid, 0)
        if child_pidfd is not None:
            os.close(child_pidfd)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=SYNC_TIMEOUT)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        assert libc.prctl(PR_SET_CHILD_SUBREAPER, previous.value, 0, 0, 0) == 0


@pytest.mark.parametrize("operation_fails", (False, True))
@pytest.mark.timeout(TEST_BACKSTOP_TIMEOUT)
def test_process_scope_preserves_failure_and_finishes_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation_fails: bool
) -> None:
    cleanup_error = OSError("injected group signaling failure")
    primary = ValueError("operation failed")
    original_killpg = os.killpg
    groups = []

    def kill_then_fail(pid: int, signum: int) -> None:
        groups.append(pid)
        with suppress(ProcessLookupError):
            original_killpg(pid, signum)
        raise cleanup_error

    monkeypatch.setattr(os, "killpg", kill_then_fail)
    process = None
    try:
        with (
            pytest.raises(ValueError if operation_fails else OSError) as raised,
            managed_process(
                [sys.executable, "-c", "import signal; print('ready',flush=True); signal.pause()"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as owner,
        ):
            process = owner.process
            assert process.stdout is not None
            assert read_line(process.stdout, timeout=SYNC_TIMEOUT) == b"ready\n"
            if operation_fails:
                raise primary
        assert raised.value is (primary if operation_fails else cleanup_error)
        assert process is not None and process.returncode is not None
        assert all(
            stream is not None and stream.closed
            for stream in (process.stdin, process.stdout, process.stderr)
        )
        assert groups == [process.pid]
        owner.close()
        assert groups == [process.pid]
        if operation_fails:
            assert any(str(cleanup_error) in note for note in primary.__notes__)
    finally:
        if process is not None:
            with suppress(ProcessLookupError):
                original_killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=SYNC_TIMEOUT)


@pytest.mark.timeout(TEST_BACKSTOP_TIMEOUT)
def test_owned_output_reader_defers_sigint_to_process_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    masks: Queue[set[int | signal.Signals]] = Queue()
    original_read = os.read
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

    def read(descriptor: int, count: int) -> bytes:
        if threading.current_thread() is not threading.main_thread():
            masks.put(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
        return original_read(descriptor, count)

    monkeypatch.setattr(os, "read", read)
    with managed_process(
        [sys.executable, "-c", "import signal; print('ready',flush=True); signal.pause()"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as owner:
        output = owner.capture_output()
        worker_mask = masks.get(timeout=SYNC_TIMEOUT)
        output.wait_for("ready", timeout=SYNC_TIMEOUT)
        assert signal.SIGINT in worker_mask
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == original_mask
    assert owner.process.returncode is not None
    assert owner.process.stdout is not None and owner.process.stdout.closed


@pytest.mark.usefixtures("defer_cyclic_gc")
@pytest.mark.timeout(TEST_BACKSTOP_TIMEOUT)
def test_process_acquisition_interrupt_reaps_created_group(monkeypatch: pytest.MonkeyPatch) -> None:
    original_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    # Popen's keyword arguments pass through unchanged.
    def launch(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original_popen(args, **kwargs)
        processes.append(process)
        assert process.stdout is not None
        assert read_line(process.stdout, timeout=SYNC_TIMEOUT) == b"ready\n"
        signal.raise_signal(signal.SIGINT)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    previous_handler = signal.getsignal(signal.SIGINT)
    try:
        with (
            pytest.raises(KeyboardInterrupt),
            managed_process(
                [sys.executable, "-c", "import signal; print('ready',flush=True); signal.pause()"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
        ):
            pytest.fail("interrupted acquisition was published")
        process = processes[0]
        assert process.returncode is not None, "acquisition abandoned the created process"
        assert process.returncode == -signal.SIGINT
        assert process.stdout is not None and process.stdout.closed
        assert signal.getsignal(signal.SIGINT) == previous_handler
    finally:
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=SYNC_TIMEOUT)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
