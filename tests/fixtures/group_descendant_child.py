# SPDX-License-Identifier: Apache-2.0

"""A ready child leaving a SIGTERM-ignoring descendant in its process group.

The leader prints its ``SENTINEL`` argument only after the descendant has
installed its disposition, so cleanup always finds a surviving group member.
"""

from __future__ import annotations

import os
import signal
import sys
import time


def main() -> None:
    sentinel = sys.argv[1]
    reader, writer = os.pipe()
    if os.fork() == 0:
        os.close(reader)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(writer, b"ready")
        os.close(writer)
        time.sleep(30)
        return
    os.close(writer)
    assert os.read(reader, len(b"ready")) == b"ready"
    os.close(reader)
    print(sentinel, flush=True)
    time.sleep(30)


if __name__ == "__main__":
    main()
