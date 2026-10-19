# SPDX-License-Identifier: Apache-2.0

"""Test-owned disposal of helper child process groups."""

import os
import signal
from collections.abc import Callable
from contextlib import suppress
from functools import partial

from zephyr_remote_openocd.helper._process import ChildProcessHandle

# A deadlock safety net for reaping a SIGKILLed leader, not an expected duration.
REAP_TIMEOUT = 5


def _kill_and_reap(process: ChildProcessHandle) -> None:
    if process.returncode is not None:
        # Once reaped, the leader's PID no longer reserves its group identity.
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=REAP_TIMEOUT)


def _kill_descendant(pidfd: int) -> None:
    with suppress(ProcessLookupError):
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)


def dispose_child_groups(
    *processes: ChildProcessHandle, descendant_pidfd: int | None = None
) -> None:
    """Kill, reap, and close test-acquired children without the code under test.

    Production disposal is what these tests exercise, so a broken disposal
    must not leave its child running. Each group is signalled through its
    leader only while that leader is unreaped; a descendant pidfd names its
    process safely at any time. Every step is attempted even after another
    fails, and the first failure is raised with the others as notes.
    """
    steps: list[Callable[[], object]] = [partial(_kill_and_reap, process) for process in processes]
    if descendant_pidfd is not None:
        steps.append(partial(_kill_descendant, descendant_pidfd))
    steps.extend(
        stream.close
        for process in processes
        for stream in (process.stdout, process.stderr)
        if stream is not None and not stream.closed
    )
    errors: list[BaseException] = []
    for step in steps:
        try:
            step()
        except BaseException as error:
            errors.append(error)
    if errors:
        for secondary in errors[1:]:
            errors[0].add_note(f"child disposal also failed: {secondary!r}")
        raise errors[0]
