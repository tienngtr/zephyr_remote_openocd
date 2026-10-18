# SPDX-License-Identifier: Apache-2.0

"""A leader that exits after starting a SIGINT- and SIGTERM-ignoring child.

The leader prints the descendant PID and ``ZRO_GDB_RTT_READY`` only after the
descendant has installed its dispositions, then exits normally.
"""

from __future__ import annotations

import os
import signal


def main() -> None:
    reader, writer = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(reader)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(writer, b"ready")
        os.close(writer)
        signal.pause()
        return
    os.close(writer)
    os.read(reader, len(b"ready"))
    os.close(reader)
    print(child, flush=True)
    print("ZRO_GDB_RTT_READY", flush=True)


if __name__ == "__main__":
    main()
