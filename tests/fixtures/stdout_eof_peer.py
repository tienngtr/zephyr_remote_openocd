# SPDX-License-Identifier: Apache-2.0

"""A helper control peer that closes stdout after READY yet stays alive.

The argument is a ``ROOT`` directory with ``eof`` and ``exit`` FIFOs. After
READY, the peer closes its only stdout once ``eof`` is released and exits,
with stderr still open until then, once ``exit`` is released.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from zephyr_remote_openocd.protocol.wire import encode_frame

from tests.control_support import CREATED


def gate(path: Path) -> None:
    with path.open("rb", buffering=0) as opened:
        assert opened.read(1) == b"x"


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
    os.write(1, CREATED)
    assert json.loads(sys.stdin.buffer.readline())["type"] == "START"
    os.write(
        1,
        encode_frame("ATTEMPT", argv=["child"])
        + encode_frame("READY", remote_address="127.64.0.1", child_pid=1),
    )
    gate(root / "eof")
    os.close(1)
    gate(root / "exit")


if __name__ == "__main__":
    main()
