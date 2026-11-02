# SPDX-License-Identifier: Apache-2.0

"""A live child that announces each argument as a line, then waits for a signal.

Each line is printed and written as readiness evidence.

It installs no handlers, so its lifetime ends only through helper disposal.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.readiness_evidence import announce


def main() -> None:
    announce(*sys.argv[1:])
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
