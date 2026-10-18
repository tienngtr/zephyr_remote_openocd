# SPDX-License-Identifier: Apache-2.0

"""Exercise initialization in fresh interpreters, outside pytest's import cache."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("missing", ("yaml", "jsonschema"))
def test_configuration_reports_missing_dependency_without_import_failure(
    tmp_path: Path, missing: str
) -> None:
    # The file is valid, so only the missing dependency can stop loading.
    config = tmp_path / "config.yaml"
    shutil.copyfile(ROOT / "resources/config.example.yaml", config)
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROBES),
            "config-without-dependency",
            str(ROOT / "python"),
            missing,
            str(config),
        ],
        cwd=tmp_path,
        check=True,
        timeout=30,
    )
