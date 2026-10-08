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
from contextlib import suppress
from pathlib import Path
from queue import Queue

import pytest

from tests.forwarding_support import RTT
from tests.hardware.test_real_rtt import TestRealRtt as RttAcceptance
from tests.hardware.test_real_semihosting import TestRealSemihosting as SemihostingAcceptance
from tests.hardware_support import PreparedTarget, RttFixture, SemihostingFixture
from tests.inventory import BuildEnvironment, InventoryHost, RttOperation, SemihostingOperation
from tests.process_support import managed_process, read_line

# Linux UAPI constants from linux/prctl.h.
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
SYNC_TIMEOUT = 30
TEST_BACKSTOP_TIMEOUT = 60

_CHILD_TREE = """
import os,signal
reader,writer=os.pipe()
child=os.fork()
if child==0:
    os.close(reader)
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    os.write(writer,b'ready')
    os.close(writer)
    signal.pause()
else:
    os.close(writer)
    os.read(reader,len(b'ready'))
    os.close(reader)
    print(child,flush=True)
    print('ZRO_GDB_RTT_READY',flush=True)
"""


@pytest.mark.parametrize("operation", ("semihosting", "rtt-abort", "blocking-flash"))
@pytest.mark.timeout(TEST_BACKSTOP_TIMEOUT)
def test_hardware_cleanup_stops_descendant_after_leader_exit(tmp_path, monkeypatch, operation):
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
    processes = []
    child_pid = None
    child_pidfd = None
    timeout = subprocess.TimeoutExpired("hardware-command", SYNC_TIMEOUT)

    def popen(*args, **kwargs):
        nonlocal child_pid, child_pidfd
        assert kwargs.get("start_new_session") is True
        process = original_popen([sys.executable, "-c", _CHILD_TREE], **kwargs)
        processes.append(process)
        child_pid = int(read_line(process.stdout, timeout=SYNC_TIMEOUT))
        child_pidfd = os.pidfd_open(child_pid)
        assert process.wait(timeout=SYNC_TIMEOUT) == 0

        def communicate(*args, **kwargs):
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
                SemihostingAcceptance().test_direct_semihosting_console_normal_completion(
                    SemihostingFixture(target, SemihostingOperation((), (), "ready", SYNC_TIMEOUT))
                )
            else:
                fixture = RttFixture(
                    target, RttOperation(RTT.local_port, "pong", "ping", SYNC_TIMEOUT, True, "main")
                )
                if operation == "rtt-abort":
                    RttAcceptance().test_debug_rtt_server_keeps_gdb_active(fixture, tmp_path)
                else:
                    RttAcceptance()._program(fixture)
        if operation != "rtt-abort":
            assert raised.value is timeout
        assert child_pid is not None and child_pidfd is not None
        poller = select.poll()
        poller.register(child_pidfd, select.POLLIN)
        assert poller.poll(SYNC_TIMEOUT * 1000), "harness left its descendant alive"
        assert os.waitpid(child_pid, 0)[0] == child_pid
        child_pid = None
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
    tmp_path, monkeypatch, operation_fails
):
    cleanup_error = OSError("injected group signaling failure")
    primary = ValueError("operation failed")
    original_killpg = os.killpg
    groups = []

    def kill_then_fail(pid, signum):
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
def test_owned_output_reader_defers_sigint_to_process_owner(monkeypatch):
    masks: Queue[set[int | signal.Signals]] = Queue()
    original_read = os.read
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

    def read(descriptor, count):
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


@pytest.mark.timeout(TEST_BACKSTOP_TIMEOUT)
def test_process_acquisition_interrupt_reaps_created_group(monkeypatch):
    original_popen = subprocess.Popen
    processes = []

    def launch(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
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
