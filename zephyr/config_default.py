#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Read the configured runner default for CMake or refresh generated metadata."""

import argparse
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

from zephyr_remote_openocd.config import ConfigError, load_config  # noqa: E402
from zephyr_remote_openocd.zephyr44.runner_state import update_runner_default  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", type=Path)
    parser.add_argument("--refresh-runner-state", type=Path)
    args = parser.parse_args()
    try:
        default = load_config(args.config).default_runner
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2
    if args.refresh_runner_state is not None:
        update_runner_default(args.refresh_runner_state, default)
    else:
        print(default)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
