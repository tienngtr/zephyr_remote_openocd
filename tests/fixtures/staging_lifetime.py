# SPDX-License-Identifier: Apache-2.0

"""Run the real helper control or stage command paused at an I/O boundary.

Hooks only pause real boundaries; both roles run the real helper. ``control``
reports ``CLEANUP_BLOCKED`` on stderr when an active stage first refuses
cleanup's exclusive lease request. ``stage WORKSPACE PAUSE RELEASE_FD`` reports
``PAUSED`` at the selected ``upload``, ``admission``, ``extraction``, or
``report`` boundary and continues after one byte arrives on the release
descriptor.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sys
import tarfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from zephyr_remote_openocd.helper import _streams as streams
from zephyr_remote_openocd.helper import _workspace as workspace
from zephyr_remote_openocd.helper.control import control
from zephyr_remote_openocd.helper.staging import stage


def run_control() -> None:
    original_flock = fcntl.flock
    announced = False

    def flock(stream: IO[bytes], operation: int) -> None:
        nonlocal announced
        try:
            original_flock(stream, operation)
        except BlockingIOError:
            if (
                not announced
                and str(stream.name).endswith(".lease")
                and operation == fcntl.LOCK_EX | fcntl.LOCK_NB
            ):
                announced = True
                print("CLEANUP_BLOCKED", file=sys.stderr, flush=True)
            raise

    # Hold helper deadlines steady during the success handshake; OS/asyncio
    # clocks remain real, and process/pipe deadlines are generous backstops.
    steady = SimpleNamespace(time=time.time, sleep=time.sleep, monotonic=lambda: 0.0)
    with patch.object(workspace, "time", steady), patch.object(fcntl, "flock", flock):
        control()


def run_stage(target: str, pause: str, release: int) -> None:
    def paused() -> None:
        print("PAUSED", file=sys.stderr, flush=True)
        os.read(release, 1)

    with ExitStack() as patches:
        if pause == "upload":
            copy = shutil.copyfileobj

            def copy_then_pause(*args: Any, **kwargs: Any) -> None:
                copy(*args, **kwargs)
                paused()

            patches.enter_context(patch.object(shutil, "copyfileobj", copy_then_pause))
        elif pause == "extraction":
            open_archive = tarfile.open

            def pause_then_open(*args: Any, **kwargs: Any) -> tarfile.TarFile:
                paused()
                return open_archive(*args, **kwargs)

            patches.enter_context(patch.object(tarfile, "open", pause_then_open))
        elif pause == "report":
            emit = streams.emit

            def pause_then_emit(*args: Any, **kwargs: Any) -> None:
                paused()
                emit(*args, **kwargs)

            patches.enter_context(patch.object(streams, "emit", pause_then_emit))
        else:
            assert pause == "admission"
            original_flock = fcntl.flock

            def flock(stream: IO[bytes], operation: int) -> None:
                if str(stream.name).endswith(".lease") and operation & fcntl.LOCK_SH:
                    paused()
                original_flock(stream, operation)

            patches.enter_context(patch.object(fcntl, "flock", flock))
        stage(target)


def main() -> None:
    role, *arguments = sys.argv[1:]
    if role == "control":
        run_control()
    else:
        assert role == "stage"
        target, pause, release = arguments
        run_stage(target, pause, int(release))


if __name__ == "__main__":
    main()
