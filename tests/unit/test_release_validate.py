# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "release_validate", ROOT / "scripts/release_validate.py"
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


def arguments(**overrides):
    values = {
        "zephyr_base": Path("/zephyr"),
        "west": Path("/west"),
        "board": "native_sim/native/64",
        "hardware_config": Path("/fixtures/hardware.toml"),
        "benchmark_build_dir": None,
        "benchmark_config": None,
        "benchmark_cwd": None,
        "benchmark_command": "flash",
        "benchmark_warmup": 5,
        "benchmark_iterations": 100,
        "require_benchmark": False,
        "python_files": ("one.py", "two.py"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_steps_has_stable_order_and_external_layers():
    steps = release.build_steps(arguments())
    assert [step.name for step in steps] == [
        "pytest",
        "ruff",
        "ruff-format",
        "pylint",
        "vermin",
        "zephyr",
        "ssh",
        "hardware",
    ]
    assert "--hardware-config" in steps[-1].command


def test_source_python_files_falls_back_without_git_metadata(tmp_path):
    (tmp_path / "source.py").write_text("pass\n")
    (tmp_path / ".scratch").mkdir()
    (tmp_path / ".scratch" / "ignored.py").write_text("pass\n")
    failed_git = SimpleNamespace(returncode=1, stdout="")
    with patch.object(release.subprocess, "run", return_value=failed_git):
        assert release.source_python_files(tmp_path) == (str(tmp_path / "source.py"),)


def test_build_steps_requires_external_evidence_inputs():
    with pytest.raises(ValueError, match="hardware-config"):
        release.build_steps(arguments(hardware_config=None))
    with pytest.raises(ValueError, match="zephyr-base"):
        release.build_steps(arguments(zephyr_base=None))
    with pytest.raises(ValueError, match="require-benchmark"):
        release.build_steps(arguments(require_benchmark=True))


def test_inventory_capability_gate_reports_missing_evidence(tmp_path):
    class Profile:
        capabilities = ("flash",)

    class Target:
        profiles = (Profile(),)

    with (
        patch("tests.inventory.load_inventory", return_value=SimpleNamespace(targets=(Target(),))),
        pytest.raises(ValueError, match="required capabilities"),
    ):
        release.validate_inventory_capabilities(tmp_path / "inventory.toml")


def test_run_steps_stops_after_first_failure_and_preserves_order():
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = f"out-{returncode}"
            self.stderr = ""

    def executor(command, **kwargs):
        calls.append((command, kwargs))
        return Result(1 if len(calls) == 2 else 0)

    results = release.run_steps(
        [
            release.Step("first", ("one",)),
            release.Step("second", ("two",)),
            release.Step("third", ("three",)),
        ],
        executor,
    )
    assert [item.name for item in results] == ["first", "second"]
    assert len(calls) == 2
    assert calls[0][1]["check"] is False


def test_summary_contract_keeps_wsl_gates_deferred():
    assert release.DEFERRED_GATES == ("PG-012", "PG-013")
