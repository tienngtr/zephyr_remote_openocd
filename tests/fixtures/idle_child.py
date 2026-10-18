# SPDX-License-Identifier: Apache-2.0

"""A live child that prints each argument as a line, then waits for a signal.

It installs no handlers, so its lifetime ends only through helper disposal.
"""

from __future__ import annotations

import signal
import sys


def main() -> None:
    for line in sys.argv[1:]:
        print(line, flush=True)
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
