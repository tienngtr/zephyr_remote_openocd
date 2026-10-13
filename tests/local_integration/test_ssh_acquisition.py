# SPDX-License-Identifier: Apache-2.0

"""Physical ownership at the raw Popen return boundary."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from types import FrameType
from typing import cast

import pytest
from zephyr_remote_openocd.controller.ssh import SshCommand

pytestmark = pytest.mark.local


@pytest.mark.usefixtures("defer_cyclic_gc")
@pytest.mark.parametrize(
    "failure_type",
    (None, MemoryError, SystemExit),
    ids=("native-sigint", "memory-error", "system-exit"),
)
def test_popen_return_interruption_reaps_transport(failure_type):
    processes: list[subprocess.Popen[bytes]] = []
    requested = threading.Event()
    delivered = threading.Event()
    stopping = threading.Event()
    previous_trace = sys.gettrace()
    previous_handler = signal.getsignal(signal.SIGINT)
    previous_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    failure = None if failure_type is None else failure_type("Popen return interrupted")

    def send_interrupt() -> None:
        if requested.wait(30) and not stopping.is_set():
            # This thread predates launch and has SIGINT unblocked. Native
            # masking on the launch thread cannot prevent Python dispatch.
            os.kill(os.getpid(), signal.SIGINT)
        delivered.set()

    sender = threading.Thread(target=send_interrupt)

    def interrupt_at_return(frame: FrameType, event: str, _arg: object):
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            process = cast(subprocess.Popen[bytes], frame.f_locals["self"])
            processes.append(process)
            assert process.pid is not None
            assert signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, set())
            if failure is not None:
                raise failure
            requested.set()
            assert delivered.wait(30), "native signal was not sent"
        return interrupt_at_return

    signal.signal(signal.SIGINT, signal.default_int_handler)
    if failure is None:
        sender.start()
    try:
        sys.settrace(interrupt_at_return)
        try:
            with pytest.raises(
                KeyboardInterrupt if failure_type is None else failure_type
            ) as raised:
                SshCommand((sys.executable, "-c", "import sys; sys.stdin.buffer.read()")).popen(
                    "unused-host", "unused-command"
                )
        finally:
            sys.settrace(previous_trace)

        if failure is not None:
            assert raised.value is failure
        assert len(processes) == 1
        process = processes[0]
        # Inspect before test-owned fallback cleanup: production must have
        # terminated, reaped, and closed every acquired pipe itself.
        assert process.returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        assert all(
            stream is not None and stream.closed
            for stream in (process.stdin, process.stdout, process.stderr)
        )
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == previous_mask - {signal.SIGINT}
    finally:
        sys.settrace(previous_trace)
        stopping.set()
        requested.set()
        if failure is None:
            sender.join(30)
            assert not sender.is_alive()
        signal.signal(signal.SIGINT, previous_handler)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
