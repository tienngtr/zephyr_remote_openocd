#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Report selection-level runner intent and its configuration path to CMake.

The arguments name the selection and state records that config_signature.py
later uses on every build.
"""

import json
import sys
from pathlib import Path


def find_module_root(start: Path) -> Path:
    """Find the Zephyr module from its manifest and Python package."""
    # nonstrict-path: a start that is not a module file reports no module root.
    resolved = start.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "zephyr" / "module.yml").is_file() and (
            candidate / "python" / "zephyr_remote_openocd" / "__init__.py"
        ).is_file():
            return candidate
    raise RuntimeError(f"cannot find Zephyr module root from {start}")


MODULE_ROOT = find_module_root(Path(__file__))
sys.path.insert(0, str(MODULE_ROOT / "python"))

from config_signature import record_selection  # noqa: E402
from zephyr_remote_openocd.config import (  # noqa: E402
    ConfigError,
    default_config_path,
    load_default_runner,
)


def main() -> int:
    try:
        config_path = str(default_config_path())
        # Record the observed state before parsing, so that a concurrent change
        # causes another regeneration rather than a missed one.
        record_selection(Path(sys.argv[1]), Path(sys.argv[2]), config_path)
        print(json.dumps({"path": config_path, "runner": load_default_runner()}))
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
