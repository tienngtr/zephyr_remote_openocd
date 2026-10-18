# SPDX-License-Identifier: Apache-2.0

"""A control helper that reports entry through a FIFO, then stays silent.

Arguments ``ENTERED CREATED READ_START`` precede the SSH host and command;
the flags are ``yes`` or ``no``. The peer ignores SIGINT, optionally writes
SESSION_CREATED and reads START, writes one byte to the ``ENTERED`` FIFO,
and waits for controller input to hang up. Without reading START it leaves
controller input unread until EOF, so a large START applies pipe backpressure
to the coordinator.
"""

from __future__ import annotations

import os
import select
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from tests.control_support import CREATED


def main() -> None:
    entered, created, read_start = sys.argv[1:4]
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if created == "yes":
        os.write(1, CREATED)
    if read_start == "yes":
        sys.stdin.buffer.readline()
    Path(entered).write_bytes(b"x")
    poller = select.poll()
    poller.register(0, select.POLLHUP)
    while not poller.poll():
        pass


if __name__ == "__main__":
    main()
