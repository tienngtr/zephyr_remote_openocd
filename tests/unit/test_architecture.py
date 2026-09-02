# SPDX-License-Identifier: Apache-2.0

"""Static dependency-layer checks, not prescribed subclass implementations."""

import ast

from tests.support import ROOT


def test_upstream_openocd_imports_stay_in_zephyr_compatibility_layer():
    for path in (ROOT / "python" / "zephyr_remote_openocd").rglob("*.py"):
        if "zephyr44" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            assert not any(
                module == "runners.openocd" or module.startswith("runners.openocd.")
                for module in modules
            ), f"Zephyr OpenOCD coupling outside compatibility layer: {path}"
