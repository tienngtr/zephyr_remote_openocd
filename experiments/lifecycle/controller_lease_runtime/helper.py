# SPDX-License-Identifier: Apache-2.0
"""Experimental helper entry: real stdin lease and stdout frames, no hardware."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .runtime import Runtime
from .unix import Subreaper
from .workspace import Workspace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    reaper = Subreaper()
    try:
        runtime = Runtime(0, 1, Workspace(args.workspace))
        asyncio.run(runtime.run())
        # Status describes helper infrastructure, never the child's exit code.
        raise SystemExit(1 if runtime.writer.failure is not None else 0)
    finally:
        reaper.close()


if __name__ == '__main__':
    main()
