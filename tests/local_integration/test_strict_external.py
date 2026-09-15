# SPDX-License-Identifier: Apache-2.0

"""Subprocess coverage for strict external-validation pytest behavior."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import ROOT

pytestmark = pytest.mark.local


def test_strict_external_turns_skips_into_failures(tmp_path: Path) -> None:
    skipping_test = tmp_path / "test_skipped_prerequisite.py"
    skipping_test.write_text(
        "import pytest\n\n"
        "def test_external_prerequisite():\n"
        "    pytest.skip('prerequisite unavailable')\n",
        encoding="utf-8",
    )
    command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "tests.conftest",
        str(skipping_test),
    )
    environment = os.environ.copy()
    environment.pop("ZRO_STRICT_EXTERNAL", None)

    ordinary = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    assert ordinary.returncode == pytest.ExitCode.OK

    environment["ZRO_STRICT_EXTERNAL"] = "1"
    strict = subprocess.run(command, cwd=ROOT, env=environment, check=False)
    assert strict.returncode == pytest.ExitCode.TESTS_FAILED
