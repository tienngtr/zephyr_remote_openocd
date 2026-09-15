# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import os
import sys

from tests.inventory import BuildEnvironment, BuildRecipe, Inventory, InventoryTarget, Toolchain
from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "validate_hardware_inventory", ROOT / "scripts/validate_hardware_inventory.py"
)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)

EXAMPLE = ROOT / "tests/fixtures/hardware.example.yaml"


def test_valid_inventory_reports_profiles(capsys) -> None:
    assert validator.main([str(EXAMPLE)]) == 0
    output = capsys.readouterr()
    assert output.out
    assert output.err == ""


def test_invalid_inventory_reports_semantic_location(tmp_path, capsys) -> None:
    path = tmp_path / "hardware.yaml"
    path.write_text(EXAMPLE.read_text().replace("host: lab", "host: missing"))
    assert validator.main([str(path)]) == 1
    assert "targets.stm32f746g_disco.host" in capsys.readouterr().err


def test_local_checks_report_missing_paths(capsys) -> None:
    assert validator.main([str(EXAMPLE), "--check-local"]) == 1
    output = capsys.readouterr()
    assert "Local check failed: build_environments.zephyr44.zephyr_base" in output.err
    assert "Local paths valid" not in output.out


def test_local_checks_accept_complete_local_paths(tmp_path, monkeypatch, capsys) -> None:
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(executable.stat().st_mode | 0o100)
    application = tmp_path / "app"
    application.mkdir()
    inventory = Inventory(
        tmp_path / "hardware.yaml",
        (BuildEnvironment("local", tmp_path, executable),),
        (Toolchain("arm", executable),),
        (),
        (
            InventoryTarget(
                "board",
                "host",
                "local",
                "arm",
                "board",
                (BuildRecipe("app", "app", "board", (), ()),),
                (),
                (),
            ),
        ),
    )
    monkeypatch.setattr(validator, "load_inventory", lambda _path: inventory)
    assert validator.main(["hardware.yaml", "--check-local"]) == 0
    assert capsys.readouterr().err == ""


def test_validation_performs_no_external_io(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("external I/O is forbidden")

    monkeypatch.setattr(os, "system", forbidden)
    assert validator.main([str(EXAMPLE)]) == 0
