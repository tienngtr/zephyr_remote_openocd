# SPDX-License-Identifier: Apache-2.0

"""Hold one helper loopback address lease from another runtime root.

Arguments are ``WORKSPACE_ROOT ADDRESS``. The holder allocates exactly
``ADDRESS``, prints it, and keeps the lease until stdin reaches EOF or the
process is killed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
from zephyr_remote_openocd.helper import _addresses as helper_addresses
from zephyr_remote_openocd.helper import _workspace as workspace_module


def main() -> None:
    workspace_root, address = Path(sys.argv[1]), sys.argv[2]
    with (
        patch.object(workspace_module, "workspace_root", lambda: workspace_root),
        patch.object(helper_addresses, "random_address", lambda: address),
    ):
        allocated = helper_addresses.allocate_listener_address(())
    print(allocated, flush=True)
    sys.stdin.buffer.read()


if __name__ == "__main__":
    main()
