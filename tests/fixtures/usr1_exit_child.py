# SPDX-License-Identifier: Apache-2.0

"""A live child whose natural successful exit is triggered by SIGUSR1.

It blocks SIGUSR1, prints ``ZRO_ARMED``, waits for SIGUSR1, then exits with the
optional status argument (default 0). The helper never requested that exit, so
it remains genuine child completion.
"""

from __future__ import annotations

import signal
import sys


def main() -> None:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    print("ZRO_ARMED", flush=True)
    signal.sigwait({signal.SIGUSR1})
    sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)


if __name__ == "__main__":
    main()
