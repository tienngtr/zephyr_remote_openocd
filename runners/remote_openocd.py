# SPDX-License-Identifier: Apache-2.0
"""Bootstrap the self-contained remote OpenOCD runner."""

import sys
from pathlib import Path


def find_module_root(start: Path) -> Path:
    """Find the Zephyr module from its manifest and Python package."""
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "zephyr" / "module.yml").is_file() and (
            candidate / "python" / "zephyr_remote_openocd" / "__init__.py"
        ).is_file():
            return candidate
    raise RuntimeError(f"cannot find Zephyr module root from {start}")


MODULE_ROOT = find_module_root(Path(__file__))
sys.path.insert(0, str(MODULE_ROOT / "python"))

from zephyr_remote_openocd.zephyr44.runner import RemoteOpenOcdBinaryRunner  # noqa: E402,F401
