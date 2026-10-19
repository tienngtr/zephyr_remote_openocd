# SPDX-License-Identifier: Apache-2.0

"""A live child whose escaped descendant keeps the child's output pipes open.

The descendant leaves the child's process group with ``setsid()``, so group
disposal cannot end it. The leader writes the descendant PID to the path in its
first argument, prints ``ZRO_ARMED``, and waits for a signal.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path


def main() -> None:
    reader, writer = os.pipe()
    descendant = os.fork()
    if descendant == 0:
        os.close(reader)
        os.setsid()
        os.write(writer, b"ready")
        os.close(writer)
        while True:
            signal.pause()
    os.close(writer)
    assert os.read(reader, len(b"ready")) == b"ready"
    os.close(reader)
    Path(sys.argv[1]).write_text(str(descendant))
    print("ZRO_ARMED", flush=True)
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
