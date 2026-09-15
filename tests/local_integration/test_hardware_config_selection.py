# SPDX-License-Identifier: Apache-2.0

"""Subprocess coverage for pytest hardware-inventory selection."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import ROOT

pytestmark = pytest.mark.local
HARDWARE_EXAMPLE = ROOT / "tests" / "fixtures" / "hardware.example.yaml"


def test_hardware_config_option_is_registered_and_overrides_environment(tmp_path: Path) -> None:
    selection_test = tmp_path / "test_inventory_selection.py"
    selection_test.write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_selected_inventory(hardware_inventory):\n"
        "    assert hardware_inventory.path == Path(os.environ['EXPECTED_INVENTORY'])\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["ZRO_HARDWARE_CONFIG"] = str(tmp_path / "wrong-inventory.yaml")
    environment["EXPECTED_INVENTORY"] = str(HARDWARE_EXAMPLE)
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.conftest",
            str(selection_test),
            "--hardware-config",
            str(HARDWARE_EXAMPLE),
        ),
        cwd=ROOT,
        env=environment,
        check=False,
    )
    assert result.returncode == pytest.ExitCode.OK
