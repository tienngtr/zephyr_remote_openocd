# SPDX-License-Identifier: Apache-2.0

"""Run the deployment bootstrap program text under injected faults.

``stale-failure OPERATION NAME...`` makes the first ``pathlib.Path`` call of
that operation on a named stale revision raise ``PermissionError``.
``serialized ROLE EVENTS_FD RELEASE_FD`` reports lock contention and target
refresh on the events descriptor; the ``first`` role holds its refresh, and
therefore its deployment lock, until one byte arrives on the release
descriptor. The bootstrap and its filesystem effects otherwise stay real.
"""

from __future__ import annotations

import fcntl
import os
import pathlib
import sys
from typing import IO, Any
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "python"))
from zephyr_remote_openocd.controller._deployment import bootstrap_program


def run(program: str) -> None:
    exec(program, {"__name__": "__main__"})


def stale_failure(operation: str, stale_names: list[str]) -> None:
    program = bootstrap_program()
    original = getattr(pathlib.Path, operation)
    failed: list[pathlib.Path] = []

    def fail_first_stale_revision(path: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
        if path.name in stale_names and not failed:
            failed.append(path)
            raise PermissionError(f"injected {operation} failure: {path}")
        return original(path, *args, **kwargs)

    with patch.object(pathlib.Path, operation, fail_first_stale_revision):
        run(program)


def serialized(role: str, events: int, release: int) -> None:
    program = bootstrap_program()
    original_flock = fcntl.flock
    original_utime = os.utime

    def acquire(lock: IO[bytes], operation: int) -> None:
        if role != "second":
            original_flock(lock, operation)
            return
        try:
            original_flock(lock, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            os.write(events, b"blocked\n")
            original_flock(lock, operation)
        else:
            os.write(events, b"unexpected-entry\n")

    def refresh(path: pathlib.Path, *args: Any, **kwargs: Any) -> None:
        os.write(events, b"refresh\n")
        if role == "first":
            assert os.read(release, 1) == b"x"
        original_utime(path, *args, **kwargs)

    with patch.object(fcntl, "flock", acquire), patch.object(os, "utime", refresh):
        run(program)


def main() -> None:
    mode, *arguments = sys.argv[1:]
    if mode == "stale-failure":
        operation, *stale_names = arguments
        stale_failure(operation, stale_names)
    else:
        assert mode == "serialized"
        role, events, release = arguments
        serialized(role, int(events), int(release))


if __name__ == "__main__":
    main()
