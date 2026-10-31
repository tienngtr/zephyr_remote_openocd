# SPDX-License-Identifier: Apache-2.0

"""Probe actual external registration in a fresh selected-source interpreter."""

from __future__ import annotations

import os
import runpy
import sys
from importlib import import_module
from pathlib import Path


def main() -> None:
    source, module, family = sys.argv[1:]
    os.environ["ZEPHYR_BASE"] = source
    sys.path.insert(0, str(Path(source) / "scripts/west_commands"))
    ZephyrBinaryRunner = import_module("runners.core").ZephyrBinaryRunner
    get_runner_cls = import_module("runners").get_runner_cls

    registration = runpy.run_path(str(Path(module) / "runners/remote_openocd.py"))
    selected = registration["RemoteOpenOcdBinaryRunner"]
    assert get_runner_cls("remote_openocd") is selected
    assert sum(cls.name() == "remote_openocd" for cls in ZephyrBinaryRunner.get_runners()) == 1
    if family == "unsupported":
        assert not any(
            name.startswith(("zephyr_remote_openocd.zephyr44.", "zephyr_remote_openocd.zephyr45."))
            for name in sys.modules
        )
    else:
        assert selected.__module__ == f"zephyr_remote_openocd.zephyr{family}._runner"
        other = "45" if family == "44" else "44"
        assert f"zephyr_remote_openocd.zephyr{other}._runner" not in sys.modules


if __name__ == "__main__":
    main()
