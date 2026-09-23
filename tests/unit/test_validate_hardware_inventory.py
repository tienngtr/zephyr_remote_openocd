# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

from tests.inventory_samples import inventory_document
from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "validate_hardware_inventory",
    ROOT / "scripts/contributor/validate_hardware_inventory.py",
)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def write_inventory(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "hardware.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_valid_inventory_reports_profiles(tmp_path, capsys) -> None:
    document = inventory_document()
    document["targets"]["summary_target"] = document["targets"].pop("target")
    profiles = document["targets"]["summary_target"]["profiles"]
    profiles["summary_profile"] = profiles.pop("profile")

    assert validator.main([str(write_inventory(tmp_path, document))]) == 0
    output = capsys.readouterr()
    assert "summary_target" in output.out
    assert "summary_profile" in output.out
    assert "flash" in output.out
    assert "debug" in output.out
    assert output.err == ""


def test_invalid_inventory_reports_semantic_location(tmp_path, capsys) -> None:
    document = inventory_document()
    document["targets"]["target"]["host"] = "missing"
    path = write_inventory(tmp_path, document)
    assert validator.main([str(path)]) == 1
    assert "targets.target.host" in capsys.readouterr().err


def test_local_checks_report_missing_paths(tmp_path, capsys) -> None:
    path = write_inventory(tmp_path, inventory_document())
    assert validator.main([str(path), "--check-local"]) == 1
    output = capsys.readouterr()
    assert "build_environments.environment.zephyr_base" in output.err
    assert "Local paths valid" not in output.out


def test_local_checks_accept_complete_local_paths(tmp_path, capsys) -> None:
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(executable.stat().st_mode | 0o100)
    for directory in ("app", "before", "mapped"):
        (tmp_path / directory).mkdir()

    document = inventory_document(zephyr_base=str(tmp_path), west=str(executable))
    document["toolchains"]["toolchain"]["gdb"] = str(executable)
    document["hosts"]["host"]["path_mappings"] = {str(tmp_path / "mapped"): "/remote"}
    path = write_inventory(tmp_path, document)

    assert validator.main([str(path), "--check-local"]) == 0
    assert capsys.readouterr().err == ""
