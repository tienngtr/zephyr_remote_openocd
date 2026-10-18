# SPDX-License-Identifier: Apache-2.0

"""Pause the real helper at workspace adoption and event-loop setup boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import IO
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from zephyr_remote_openocd.helper import _workspace as workspace_module
from zephyr_remote_openocd.helper import control as helper_control

SignalHandler = Callable[[int, FrameType | None], object] | int | None


def report(record: dict[str, object]) -> None:
    # The parent can signal as soon as it reads the checkpoint. A handler's
    # acknowledgement must not reenter a still-locked buffered stderr writer.
    os.write(2, json.dumps(record).encode() + b"\n")


def main() -> None:
    boundary, action, gate = sys.argv[1:]
    original_new = workspace_module.new_workspace
    original_queue = asyncio.Queue
    original_loop = asyncio.events.new_event_loop
    original_signal = signal.signal
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    acquired: list[tuple[str, Path, IO[bytes]]] = []
    paused = False

    def pause() -> None:
        nonlocal paused
        if paused:
            return
        paused = True
        assert acquired
        report({"workspace": str(acquired[0][1])})
        if action == "exception":
            raise RuntimeError("coordinator setup failed")
        os.read(int(gate), 1)

    def allocate() -> tuple[str, Path, IO[bytes]]:
        resources = original_new()
        acquired.append(resources)
        if boundary == "allocated":
            pause()
        return resources

    def queue[T](maxsize: int = 0) -> asyncio.Queue[T]:
        if boundary == "constructor":
            pause()
        return original_queue(maxsize)

    def loop() -> asyncio.AbstractEventLoop:
        if boundary == "loop":
            pause()
        return original_loop()

    def install(signum: int, handler: SignalHandler) -> SignalHandler:
        if not acquired and callable(handler):

            def observed(number: int, frame: FrameType | None) -> None:
                handler(number, frame)
                report({"signal": number})

            return original_signal(signum, observed)
        return original_signal(signum, handler)

    status = 0
    with (
        patch.object(workspace_module, 'new_workspace', allocate),
        patch.object(asyncio, "Queue", queue),
        patch.object(asyncio.events, "new_event_loop", loop),
        patch.object(signal, "signal", install),
    ):
        try:
            helper_control.control()
        except BaseException as error:
            status = 1
            failure = error.__cause__ or error
            report({"failure": type(failure).__name__})
        finally:
            assert acquired
            _session_id, workspace, lock = acquired[0]
            report(
                {
                    "workspace_remaining": workspace.exists(),
                    "lease_remaining": workspace_module.lease_path(workspace).exists(),
                    "lock_closed": lock.closed,
                    "handlers_restored": all(
                        signal.getsignal(signum) == handler
                        for signum, handler in previous_handlers.items()
                    ),
                }
            )
    raise SystemExit(status)


if __name__ == "__main__":
    main()
