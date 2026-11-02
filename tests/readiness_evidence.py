# SPDX-License-Identifier: Apache-2.0

"""Emulate the runner's readiness Tcl from a synthetic helper child.

The helper starts its child in ``<workspace>/staged`` and owns the readiness
artifact at ``<workspace>/readiness``. Each line is also printed, because some
tests use it as an output handshake.
"""

from __future__ import annotations

from pathlib import Path

ARTIFACT = Path("..", "readiness")


def announce(*lines: str) -> None:
    for line in lines:
        print(line, flush=True)
    if ARTIFACT.is_file():
        with ARTIFACT.open("a", encoding="utf-8") as artifact:
            artifact.writelines(f"{line}\n" for line in lines)
