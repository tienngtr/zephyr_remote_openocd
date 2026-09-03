#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Print the configured runner default for CMake."""

import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_ROOT / "python"))

from zephyr_remote_openocd.config import ConfigError, load_config  # noqa: E402

try:
    print(load_config(Path(sys.argv[1])).default)
except ConfigError as error:
    print(error, file=sys.stderr)
    raise SystemExit(2) from error
