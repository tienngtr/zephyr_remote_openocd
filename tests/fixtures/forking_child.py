# SPDX-License-Identifier: Apache-2.0

"""A child leader whose SIGTERM-ignoring descendant outlives it.

Arguments are ``LEADER PID_PATH``. The descendant writes its PID to
``PID_PATH``, closes its output, and ignores SIGTERM. With ``exits-on-term``
the leader exits cleanly on SIGTERM; with ``exits-on-usr1`` it exits cleanly
only on SIGUSR1 and otherwise waits in ``signal.pause()``.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from types import FrameType


def stop(_signal: int, _frame: FrameType | None) -> None:
    raise SystemExit(0)


def main() -> None:
    leader, pid_path = sys.argv[1:]
    exits_on_term = leader == "exits-on-term"
    if not exits_on_term:
        assert leader == "exits-on-usr1"
    signal.signal(signal.SIGTERM if exits_on_term else signal.SIGUSR1, stop)
    if os.fork() == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        descriptor = os.open(pid_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        os.close(1)
        os.close(2)
        time.sleep(30)
        return
    if exits_on_term:
        time.sleep(30)
    else:
        signal.pause()


if __name__ == "__main__":
    main()
