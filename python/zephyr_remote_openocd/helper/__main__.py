# SPDX-License-Identifier: Apache-2.0

"""Execute the helper from a checkout or its self-contained zip application."""

import sys
from pathlib import Path

if not __package__ and Path(__file__).is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zephyr_remote_openocd.helper.cli import run

if __name__ == "__main__":
    run()
