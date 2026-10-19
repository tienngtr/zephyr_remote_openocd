# SPDX-License-Identifier: Apache-2.0

"""A live child that closes its stdout, then waits for a signal.

Its stdout relay reaches EOF while the child itself stays alive until helper
disposal ends it.
"""

from __future__ import annotations

import os
import signal


def main() -> None:
    os.close(1)
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
