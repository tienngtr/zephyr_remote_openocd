# SPDX-License-Identifier: Apache-2.0

"""External import failures are contained without masking adapter defects."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import ROOT, env_path

pytestmark = pytest.mark.zephyr


@pytest.mark.parametrize(
    "fault",
    (
        "missing-source-module",
        "missing-source-export",
        "adapter-import-error",
        "adapter-attribute-error",
    ),
)
def test_external_runner_import_failure_boundary(tmp_path: Path, fault: str) -> None:
    source = env_path("ZEPHYR_BASE")
    if source is None or not source.is_dir():
        pytest.skip("ZEPHYR_BASE must name a supported stable Zephyr source tree")
    pytest.importorskip("west")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests/fixtures/runner_selection_probe.py"),
            str(source),
            str(ROOT),
            fault,
        ],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout
