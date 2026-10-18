# SPDX-License-Identifier: Apache-2.0

"""A configured-SSH replacement whose stderr far exceeds pipe capacity.

It serves a protocol-shaped helper control session, an empty staging upload,
and a forwarding readiness command, writing a large stderr burst ending in a
recognizable tail before control and forwarding output.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import sys
import tarfile


def noisy_stderr(marker: bytes) -> None:
    sys.stderr.buffer.write(b"prefix-" + b"x" * 262144 + marker + b"\n")
    sys.stderr.flush()


def emit(message: dict[str, object]) -> None:
    print(json.dumps({"version": 1, **message}), flush=True)


def control() -> None:
    noisy_stderr(b"control-tail")
    emit(
        {
            "type": "SESSION_CREATED",
            "helper": "fake",
            "session_id": "session",
            "remote_workspace": "/tmp/fake-workspace",
        }
    )
    for line in sys.stdin.buffer:
        if json.loads(line)["type"] == "START":
            emit({"type": "ATTEMPT", "argv": ["test-process"]})
            emit({"type": "READY", "remote_address": "127.64.0.1", "child_pid": 1})
    emit(
        {
            "type": "SESSION_ENDED",
            "trigger": "controller_eof",
            "primary_failure": None,
            "diagnostics": [],
            "child_result": None,
            "cleanup": {
                "child_disposal": "confirmed",
                "workspace_disposal": "confirmed",
                "residual_resources": [],
            },
        }
    )


def stage() -> None:
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as archive:
        assert archive.getmembers() == []
    emit(
        {
            "type": "STAGED",
            "files": [],
            "directories": [],
            "byte_count": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
        }
    )


def forward(remote_command: str) -> None:
    noisy_stderr(b"forward-tail")
    sentinel = re.search(r"ZRO_FORWARD_[0-9a-f]+", remote_command)
    assert sentinel is not None
    print(sentinel.group(0), flush=True)
    sys.stdin.buffer.read()


def main() -> None:
    remote_command = sys.argv[-1]
    if remote_command.endswith(" control"):
        control()
    elif shlex.split(remote_command)[2] == "stage":
        stage()
    elif remote_command.startswith("python3 -c "):
        forward(remote_command)


if __name__ == "__main__":
    main()
