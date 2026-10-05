# SPDX-License-Identifier: Apache-2.0

"""Execute generated SSH remote commands in a disposable local test account."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1])
    assert sys.argv[2] == "test-host"
    argv = shlex.split(sys.argv[3])
    assert argv[0] == "python3"
    argv[0] = sys.executable
    environment = os.environ.copy()
    environment["HOME"] = str(root / "home")
    environment["XDG_RUNTIME_DIR"] = str(root / "runtime")
    os.execve(sys.executable, argv, environment)


if __name__ == "__main__":
    main()
