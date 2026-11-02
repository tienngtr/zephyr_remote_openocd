# SPDX-License-Identifier: Apache-2.0

"""A live child whose natural successful exit is triggered by SIGUSR1.

It blocks SIGUSR1, announces ``ZRO_ARMED``, waits for SIGUSR1, then exits with the
optional status argument (default 0). The helper never requested that exit, so
it remains genuine child completion.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.readiness_evidence import announce


def main() -> None:
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    announce("ZRO_ARMED")
    signal.sigwait({signal.SIGUSR1})
    sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)


if __name__ == "__main__":
    main()
