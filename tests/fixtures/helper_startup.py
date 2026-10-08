# SPDX-License-Identifier: Apache-2.0

"""Pause the real helper at workspace adoption and event-loop setup boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import IO
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from zephyr_remote_openocd import remote_helper as helper


def main() -> None:
    boundary, action, gate = sys.argv[1:]
    original_new = helper.new_workspace
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
        print(json.dumps({"workspace": str(acquired[0][1])}), file=sys.stderr, flush=True)
        if action == "exception":
            raise RuntimeError("coordinator setup failed")
        os.read(int(gate), 1)

    def allocate():
        resources = original_new()
        acquired.append(resources)
        if boundary == "allocated":
            pause()
        return resources

    def queue(*args, **kwargs):
        if boundary == "constructor":
            pause()
        return original_queue(*args, **kwargs)

    def loop():
        if boundary == "loop":
            pause()
        return original_loop()

    def install(signum, handler):
        if not acquired and callable(handler):

            def observed(number, frame):
                handler(number, frame)
                print(json.dumps({"signal": number}), file=sys.stderr, flush=True)

            return original_signal(signum, observed)
        return original_signal(signum, handler)

    status = 0
    with (
        patch.object(helper, "new_workspace", allocate),
        patch.object(asyncio, "Queue", queue),
        patch.object(asyncio.events, "new_event_loop", loop),
        patch.object(signal, "signal", install),
    ):
        try:
            helper.control()
        except BaseException as error:
            status = 1
            failure = error.__cause__ or error
            print(json.dumps({"failure": type(failure).__name__}), file=sys.stderr, flush=True)
        finally:
            assert acquired
            _session_id, workspace, lock = acquired[0]
            print(
                json.dumps(
                    {
                        "workspace_remaining": workspace.exists(),
                        "lease_remaining": helper._lease_path(workspace).exists(),
                        "lock_closed": lock.closed,
                        "handlers_restored": all(
                            signal.getsignal(signum) == handler
                            for signum, handler in previous_handlers.items()
                        ),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
    raise SystemExit(status)


if __name__ == "__main__":
    main()
