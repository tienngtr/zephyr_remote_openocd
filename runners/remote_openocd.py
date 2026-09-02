# SPDX-License-Identifier: Apache-2.0
"""Bootstrap the self-contained prototype runner."""

from pathlib import Path
import sys


MODULE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_ROOT / "python"))

from zephyr_remote_openocd.zephyr44.runner import RemoteOpenOcdBinaryRunner  # noqa: E402,F401

