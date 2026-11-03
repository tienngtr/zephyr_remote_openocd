# SPDX-License-Identifier: Apache-2.0

"""Keep subprocess coverage out of Zephyr's own build scripts in CI.

Coverage's ``patch = subprocess`` starts the C tracer in every Python child.
Zephyr's CMake configuration runs its Kconfig, devicetree, and helper scripts
as such children; none of them can execute measured code, but tracing them
roughly doubles each configuration. CI installs this module with a ``.pth``
file that sorts before coverage's own, so a child whose main script lies in a
Zephyr tree's ``scripts`` directory drops the coverage request before coverage
reads it. That also keeps the request from its descendants. All other
children, including ``west`` and the module's CMake-time scripts, are measured
as before.
"""

import os
import sys
from pathlib import Path

_COVERAGE_REQUESTS = ("COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START")


def _runs_zephyr_script() -> bool:
    script = sys.argv[0] if sys.argv else ""
    if script in ("", "-c", "-m"):
        return False
    for directory in Path(script).resolve().parents:
        if directory.name == "scripts" and (directory.parent / "zephyr-env.sh").is_file():
            return True
    return False


if _runs_zephyr_script():
    for _name in _COVERAGE_REQUESTS:
        os.environ.pop(_name, None)
