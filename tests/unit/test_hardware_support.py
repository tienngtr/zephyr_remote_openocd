# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.config import load_config, resolve_remote

from tests.hardware_support import HardwarePreparation, _record, records_for
from tests.inventory import load_inventory
from tests.support import ROOT


def test_inventory_profiles_become_independent_capability_records(tmp_path):
    inventory = load_inventory(ROOT / "tests/fixtures/hardware.example.toml")
    host = inventory.host("lab")
    target = inventory.target("board")
    profile = next(item for item in target.profiles if item.name == "debug")
    record = _record(target, host, profile, tmp_path / "build", tmp_path / "config.yaml")
    assert {"debug", "attach", "debugserver"}.issubset(record["capabilities"])
    assert records_for([record], "rtt") == []
    assert records_for([record], "debug") == [record]
    assert record["debug_breakpoint"] == "main"
    assert "serial_device" not in record


def test_probe_serial_is_translated_to_runner_argument(tmp_path):
    inventory = load_inventory(ROOT / "tests/fixtures/hardware.example.toml")
    host = inventory.host("lab")
    target = inventory.target("board")
    profile = replace(
        next(item for item in target.profiles if item.name == "flash"),
        probe_serial="example-probe",
    )
    record = _record(target, host, profile, tmp_path / "build", tmp_path / "config.yaml")
    assert "--serial=example-probe" in record["runner_args"]
    assert record["precondition_build_dir"] == str(tmp_path / "minimal")
    assert record["quiescence_timeout"] == 2


def test_preparation_builds_only_requested_recipes_and_caches_success(tmp_path, monkeypatch):
    inventory = load_inventory(ROOT / "tests/fixtures/hardware.example.toml")
    original = inventory.target("board")
    target = replace(original, zephyr_base=tmp_path, west=Path(sys.executable))
    # An unavailable unrelated target and recipe must not affect selection.
    unrelated = replace(original, id="unavailable")
    extra = replace(target.builds[0], name="unused", application="/unavailable/application")
    unused_profile = replace(target.profiles[0], name="unused", build="unused")
    target = replace(
        target, builds=(*target.builds, extra), profiles=(*target.profiles, unused_profile)
    )
    inventory = replace(inventory, targets=(unrelated, target))
    build_root = tmp_path / "builds"
    config_root = tmp_path / "configs"
    build_root.mkdir()
    config_root.mkdir()
    preparation = HardwarePreparation(inventory, build_root, config_root)
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "developer-remote")
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", "/developer/config.yaml")
    with patch("tests.hardware_support.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1, stdout="build failed")
        with pytest.raises(pytest.fail.Exception, match="build failed"):
            preparation.prepare("board:flash")
        run.return_value = SimpleNamespace(returncode=0, stdout="")
        flash = preparation.prepare("board:flash")
        debug = preparation.prepare("board:debug")
    # Failed attempts are retried; the successful flash preparation builds both
    # the intended and precondition recipes, and debug reuses the intended one.
    assert run.call_count == 3
    assert flash["build_dir"] == debug["build_dir"]
    assert flash["id"] != debug["id"]
    environment = run.call_args.kwargs["env"]
    assert "ZEPHYR_REMOTE_OPENOCD_REMOTE" not in environment
    assert environment["ZEPHYR_REMOTE_OPENOCD_CONFIG"] == flash["config_path"]
    selected = resolve_remote(load_config(Path(flash["config_path"])), remote_name="lab")
    assert selected.ssh_host == inventory.host("lab").address
    assert not (build_root / "unavailable").exists()
    assert not (build_root / "board" / "unused").exists()
