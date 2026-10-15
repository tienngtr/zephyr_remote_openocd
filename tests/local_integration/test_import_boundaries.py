# SPDX-License-Identifier: Apache-2.0

"""Exercise initialization in fresh interpreters, outside pytest's import cache."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.support import ROOT


def test_package_initializers_do_not_load_implementation_modules(tmp_path: Path) -> None:
    script = """
import importlib
import sys

sys.path.insert(0, sys.argv[1])
packages = {
    "zephyr_remote_openocd",
    *(f"zephyr_remote_openocd.{name}" for name in (
        "controller", "openocd", "protocol", "helper", "zephyr44",
    )),
}
for name in sorted(packages):
    importlib.import_module(name)
loaded = {name for name in sys.modules if name.startswith("zephyr_remote_openocd")}
assert loaded == packages, loaded - packages
"""
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT / "python")],
        cwd=tmp_path,
        check=True,
        timeout=30,
    )


def test_controller_facade_stays_within_runtime_dependency_boundary(tmp_path: Path) -> None:
    script = """
import importlib
import sys

sys.path.insert(0, sys.argv[1])
# Fail even for attempted optional imports, rather than checking only what was
# left in sys.modules. These packages are not dependencies of session execution.
class ForbiddenImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"runners", "yaml", "jsonschema", "elftools"}:
            raise AssertionError(f"unexpected dependency: {fullname}")
        if any(fullname == name or fullname.startswith(name + ".") for name in (
            "zephyr_remote_openocd.config", "zephyr_remote_openocd.helper",
            "zephyr_remote_openocd.openocd", "zephyr_remote_openocd.zephyr44",
        )):
            raise AssertionError(f"unexpected dependency: {fullname}")

sys.meta_path.insert(0, ForbiddenImports())
# Session imports may define effectful operations but must not execute them.
def reject_effect(event, args):
    if event in {"subprocess.Popen", "socket.__new__", "os.system"}:
        raise AssertionError(f"import-time effect: {event}")

sys.addaudithook(reject_effect)
api = importlib.import_module("zephyr_remote_openocd.controller.api")
assert api.SessionController is not None
"""
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT / "python")],
        cwd=tmp_path,
        check=True,
        timeout=30,
    )
