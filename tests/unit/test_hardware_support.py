# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace

from tests.hardware_support import _record, records_for
from tests.inventory import load_inventory
from tests.support import ROOT


def test_inventory_profiles_become_independent_capability_records(tmp_path):
    inventory = load_inventory(ROOT / "tests/fixtures/hardware.example.toml")
    host = inventory.host("lab")
    target = inventory.target("board")
    profile = next(item for item in target.profiles if item.name == "debug")
    record = _record(target, host, profile, tmp_path / "build", tmp_path / "config.toml")
    assert {"debug", "attach", "debugserver"}.issubset(record["capabilities"])
    assert records_for([record], "rtt") == []
    assert records_for([record], "debug") == [record]
    assert record["serial_data_bits"] == 8
    assert record["serial_parity"] == "none"


def test_probe_serial_is_translated_to_runner_argument(tmp_path):
    inventory = load_inventory(ROOT / "tests/fixtures/hardware.example.toml")
    host = inventory.host("lab")
    target = inventory.target("board")
    profile = replace(
        next(item for item in target.profiles if item.name == "flash"),
        probe_serial="example-probe",
    )
    record = _record(target, host, profile, tmp_path / "build", tmp_path / "config.toml")
    assert "--serial=example-probe" in record["runner_args"]
