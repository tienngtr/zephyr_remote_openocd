# SPDX-License-Identifier: Apache-2.0

"""A ready external child whose active lifetime ends only when its gate opens."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1])
    channel = int(
        next(
            argument.removeprefix("test_channel ")
            for argument in sys.argv
            if argument.startswith("test_channel ")
        )
    )
    assert "set _ZEPHYR_BOARD_SERIAL test-probe" in sys.argv
    print("ZRO_TEST_READY", flush=True)
    with (root / f"entered-{channel}").open("wb", buffering=0) as entered:
        entered.write(
            (json.dumps({"pid": os.getpid(), "workspace": str(Path.cwd().parent)}) + "\n").encode()
        )
    with (root / f"gate-{channel}").open("rb", buffering=0) as gate:
        assert gate.read(1) == b"x"


if __name__ == "__main__":
    main()
