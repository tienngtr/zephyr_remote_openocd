# SPDX-License-Identifier: Apache-2.0

"""A helper control peer whose forked descendant holds stdout after exit.

The argument is a ``ROOT`` directory of FIFOs and files prepared by the test.
The descendant acknowledges ``H`` on ``ROOT/ack``, then for each ``w`` read
from ``ROOT/hold`` writes ``ROOT/noise`` to the shared stdout and
acknowledges ``W``. Any other byte or EOF closes stdout and acknowledges
``D``. The leader writes ``ROOT/initial``, optionally reads START and writes
``ROOT/final``, optionally waits on ``ROOT/exit_gate``, and exits with the
status in ``ROOT/status``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import suppress
from pathlib import Path


def hold_stdout(root: Path) -> None:
    os.close(0)
    os.close(2)
    with (
        (root / "hold").open("rb", buffering=0) as hold,
        (root / "ack").open("wb", buffering=0) as ack,
    ):
        ack.write(b"H")
        while hold.read(1) == b"w":
            with suppress(BrokenPipeError):
                os.write(1, (root / "noise").read_bytes())
            ack.write(b"W")
        os.close(1)
        ack.write(b"D")


def main() -> None:
    root = Path(sys.argv[1])
    if " stage " in sys.argv[-1]:
        sys.stdin.buffer.read()
        staged = dict(
            version=1,
            type="STAGED",
            files=[],
            directories=[],
            byte_count=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        )
        print(json.dumps(staged), flush=True)
        sys.exit(0)
    if os.fork() == 0:
        try:
            hold_stdout(root)
        finally:
            os._exit(0)
    os.write(1, (root / "initial").read_bytes())
    if (root / "after_start").exists():
        assert json.loads(sys.stdin.buffer.readline())["type"] == "START"
        os.write(1, (root / "final").read_bytes())
    if (root / "exit_gate").exists():
        with (root / "exit_gate").open("rb", buffering=0) as gate:
            assert gate.read(1) == b"x"
    os._exit(int((root / "status").read_text()))


if __name__ == "__main__":
    main()
