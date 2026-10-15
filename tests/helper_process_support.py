# SPDX-License-Identifier: Apache-2.0

"""Shared rollback for test-owned supervised OpenOCD processes."""

import asyncio
import os
import signal
from contextlib import suppress

from zephyr_remote_openocd.helper._process import SupervisedProcess


def cleanup_supervised_process(child: SupervisedProcess, descendant_pidfd: int | None) -> None:
    try:
        if child.poll() is None:
            asyncio.run(child.terminate())
        child.close_streams()
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        with suppress(BaseException):
            child.process.wait(timeout=5)
        with suppress(BaseException):
            child.close_streams()
    if descendant_pidfd is not None:
        with suppress(ProcessLookupError):
            signal.pidfd_send_signal(descendant_pidfd, signal.SIGKILL)
