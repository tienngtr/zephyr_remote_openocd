# SPDX-License-Identifier: Apache-2.0

"""A live child that records staging closure when its cleanup signal arrives.

Arguments are the closure-marker path and a result path. After installing its
SIGTERM handler the child prints ``ZRO_ARMED``; on SIGTERM it records whether
the marker already exists, then exits.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path
from types import FrameType


def main() -> None:
    marker, result = Path(sys.argv[1]), Path(sys.argv[2])

    def witness(_signum: int, _frame: FrameType | None) -> None:
        result.write_text("closed" if marker.exists() else "open")
        sys.exit(0)

    signal.signal(signal.SIGTERM, witness)
    print("ZRO_ARMED", flush=True)
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
