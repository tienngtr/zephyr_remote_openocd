# SPDX-License-Identifier: Apache-2.0

"""Subprocess coverage for the required external-tests pytest option."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import ROOT

pytestmark = pytest.mark.local


def test_require_external_tests_turns_skips_into_failures(tmp_path: Path) -> None:
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
    ordinary = subprocess.run(command, cwd=ROOT, check=False)
    assert ordinary.returncode == pytest.ExitCode.OK

    required = subprocess.run(
        (*command, "--require-external-tests"),
        cwd=ROOT,
        check=False,
    )
    assert required.returncode == pytest.ExitCode.TESTS_FAILED
