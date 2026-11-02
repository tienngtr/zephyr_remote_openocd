# SPDX-License-Identifier: Apache-2.0

"""A synthetic OpenOCD that writes a deterministic byte pattern to its log.

Arguments: the log channel as the helper materialized it (one Tcl word), a
byte count, and ``exit`` or ``wait``. After writing, the child announces
``written``, then exits successfully or waits for a signal.
"""

from __future__ import annotations

import re
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.readiness_evidence import announce


def pattern(count: int) -> bytes:
    return (bytes(range(256)) * (count // 256 + 1))[:count]


def main() -> None:
    word, count, then = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    # Undo the helper's double-quoted Tcl word, as OpenOCD's Tcl would.
    path = re.sub(r"\\(.)", r"\1", word[1:-1])
    with open(path, "wb", buffering=0) as log:
        data = memoryview(pattern(count))
        while data:
            data = data[log.write(data[: 64 * 1024]) :]
    announce("written")
    if then == "exit":
        return
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
