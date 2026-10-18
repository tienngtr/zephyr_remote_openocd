# SPDX-License-Identifier: Apache-2.0

"""A configured-SSH replacement whose helper session ends during forwarding.

Arguments are ``ROOT`` followed by SSH arguments. After READY, the control
session waits on the ``ROOT/gate`` FIFO, then ends as ``ROOT/terminal``
selects: an exit status, ``error`` for a helper failure, or ``reader`` for an
invalid frame. Staging, forwarding readiness, and deployment are answered
without effects.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


def emit(kind: str, **fields: object) -> None:
    print(json.dumps(dict(version=1, type=kind, **fields)), flush=True)


def control(root: Path) -> None:
    emit("SESSION_CREATED", helper="peer", session_id="session", remote_workspace="/workspace")
    request = json.loads(sys.stdin.buffer.readline())
    emit("ATTEMPT", argv=request["argv"])
    emit("READY", remote_address="127.64.0.1", child_pid=1)
    with (root / "gate").open("rb", buffering=0) as gate:
        assert gate.read(1) == b"x"
    terminal = (root / "terminal").read_text()
    if terminal == "reader":
        print("invalid helper frame", flush=True)
        sys.exit(0)
    error = (
        dict(code="CLEANUP_FAILED", message="failure during forwarding", diagnostics=[])
        if terminal == "error"
        else None
    )
    emit(
        "SESSION_ENDED",
        trigger="helper_failure" if error else "child_exit",
        primary_failure=error,
        diagnostics=[],
        child_result=None if error else dict(returncode=int(terminal), termination_requested=False),
        cleanup=dict(
            child_disposal="confirmed", workspace_disposal="confirmed", residual_resources=[]
        ),
    )


def main() -> None:
    root = Path(sys.argv[1])
    command = sys.argv[-1]
    forward = re.search(r"ZRO_FORWARD_[0-9a-f]+", command)
    if command.endswith(" control"):
        control(root)
    elif " stage " in command:
        sys.stdin.buffer.read()
        emit(
            "STAGED",
            byte_count=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            files=[],
            directories=[],
        )
    elif forward is not None:
        print(forward.group(0), flush=True)
        sys.stdin.buffer.read()
    else:
        content = sys.stdin.buffer.read()
        emit(
            "DEPLOYED",
            status="reused",
            path="/helper.py",
            sha256=hashlib.sha256(content).hexdigest(),
        )


if __name__ == "__main__":
    main()
