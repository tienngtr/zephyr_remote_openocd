# SPDX-License-Identifier: Apache-2.0

"""Exercise initialization in fresh interpreters, outside pytest's import cache."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.support import ROOT

PROBES = ROOT / "tests/fixtures/import_probes.py"


def test_package_initializers_do_not_load_implementation_modules(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, "-I", str(PROBES), "initializers", str(ROOT / "python")],
        cwd=tmp_path,
        check=True,
        timeout=30,
    )


def test_controller_facade_stays_within_runtime_dependency_boundary(tmp_path: Path) -> None:
    subprocess.run(
        [sys.executable, "-I", str(PROBES), "controller-facade", str(ROOT / "python")],
        cwd=tmp_path,
        check=True,
        timeout=30,
    )
