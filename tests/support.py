# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_repository_root(start: Path) -> Path:
    """Find the module root from stable repository markers."""
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "zephyr" / "module.yml").is_file() and (
            candidate / "python" / "zephyr_remote_openocd"
        ).is_dir():
            return candidate
    raise RuntimeError(f"cannot find repository root from {start}")


ROOT = find_repository_root(Path(__file__))
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else None


def is_wsl2() -> bool:
    if sys.platform != "linux":
        return False
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text().lower()
        version = Path("/proc/version").read_text().lower()
    except OSError:
        return False
    return "microsoft" in release and "wsl2" in version
