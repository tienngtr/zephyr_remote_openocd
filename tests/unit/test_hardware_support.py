# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from zephyr_remote_openocd.config import load_config, resolve_remote

from tests.elf_fixtures import (
    ELF_LOAD_VADDR_OFFSET,
    ELF_PADDING_OFFSET,
    ELF_SHIFTED_LOAD_VADDR,
    ELF_WITNESS_ADDRESS,
    ELF_WITNESS_BYTES,
    ELF_WITNESS_OFFSET,
    elf_memory_witness_bytes,
)
from tests.hardware_support import (
    DebugFixture,
    FlashFixture,
    HardwarePreparation,
    elf_memory_witness,
)
from tests.inventory import Inventory, load_inventory
from tests.inventory_samples import inventory_document


def _preparation_with_unavailable_recipe(
    tmp_path: Path,
) -> tuple[HardwarePreparation, Inventory, Path]:
    inventory_path = tmp_path / "hardware.yaml"
    inventory_path.write_text(
        yaml.safe_dump(
            inventory_document(zephyr_base=str(tmp_path), west=sys.executable),
            sort_keys=False,
        )
    )
    inventory = load_inventory(inventory_path)
    original = inventory.target("target")
    build_environment = inventory.build_environment("environment")
    target = original
    # An unavailable unrelated target and recipe must not affect selection.
    unavailable_environment = replace(
        build_environment,
        name="unavailable",
        zephyr_base=tmp_path / "unavailable",
    )
    unrelated = replace(original, name="unavailable", build_environment="unavailable")
    extra = replace(target.builds[0], name="unused", application="/unavailable/application")
    unused_profile = replace(target.profile("profile"), name="unused", build="unused")
    target = replace(
        target, builds=(*target.builds, extra), profiles=(*target.profiles, unused_profile)
    )
    inventory = replace(
        inventory,
        build_environments=(unavailable_environment, build_environment),
        targets=(unrelated, target),
    )
    build_root = tmp_path / "builds"
    config_root = tmp_path / "configs"
    build_root.mkdir()
    config_root.mkdir()
    return HardwarePreparation(inventory, build_root, config_root), inventory, build_root


def test_preparation_builds_only_requested_recipes(tmp_path):
    preparation, _inventory, build_root = _preparation_with_unavailable_recipe(tmp_path)
    with patch("tests.hardware_support.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "")
        preparation.prepare("target:profile", "flash")

    assert run.call_count == 2
    commands = [call.args[0] for call in run.call_args_list]
    assert all(not any("/unavailable" in value for value in command) for command in commands)
    build_dirs = {command[command.index("-d") + 1] for command in commands}
    assert build_dirs == {
        str(build_root / "target" / "application"),
        str(build_root / "target" / "precondition"),
    }
    assert not (build_root / "unavailable").exists()
    assert not (build_root / "target" / "unused").exists()


def test_preparation_retries_failed_build_and_caches_success(tmp_path, monkeypatch):
    preparation, inventory, build_root = _preparation_with_unavailable_recipe(tmp_path)
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "developer_remote")
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", "/developer/config.yaml")
    with patch("tests.hardware_support.subprocess.run") as run:
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "build failed"),
            subprocess.CompletedProcess([], 0, ""),
            subprocess.CompletedProcess([], 0, ""),
        ]
        with pytest.raises(pytest.fail.Exception, match="build failed"):
            preparation.prepare("target:profile", "flash")
        flash = preparation.prepare("target:profile", "flash")
        debug = preparation.prepare("target:profile", "debug")

    assert run.call_count == 3
    assert isinstance(flash, FlashFixture)
    assert isinstance(debug, DebugFixture)
    assert flash.target.build_dir == debug.target.build_dir
    assert flash.target.id == debug.target.id
    assert "--serial=probe" in flash.target.runner_args
    assert flash.precondition_build_dir == build_root / "target" / "precondition"
    assert flash.operation.quiescence_timeout == 2
    environment = run.call_args.kwargs["env"]
    assert "ZEPHYR_REMOTE_OPENOCD_REMOTE" not in environment
    assert environment["ZEPHYR_REMOTE_OPENOCD_CONFIG"] == str(flash.target.config_path)
    selected = resolve_remote(load_config(flash.target.config_path), remote_name="host")
    assert selected.ssh_host == inventory.host("host").ssh_host


def test_elf_memory_witness_finds_bytes_that_distinguish_images(tmp_path: Path) -> None:
    original = elf_memory_witness_bytes()
    selected_data = bytearray(original)
    selected_data[ELF_WITNESS_OFFSET] ^= 0xFF
    struct.pack_into(
        "<Q",
        selected_data,
        ELF_LOAD_VADDR_OFFSET,
        ELF_SHIFTED_LOAD_VADDR,
    )
    before_path = tmp_path / "before.elf"
    selected_path = tmp_path / "selected.elf"
    before_path.write_bytes(original)
    selected_path.write_bytes(selected_data)

    address, before, selected = elf_memory_witness(before_path, selected_path)
    assert address == ELF_WITNESS_ADDRESS
    assert before == ELF_WITNESS_BYTES[:16]
    assert selected == bytes((ELF_WITNESS_BYTES[0] ^ 0xFF, *ELF_WITNESS_BYTES[1:16]))


def test_elf_memory_witness_ignores_load_segment_padding(tmp_path: Path) -> None:
    original = elf_memory_witness_bytes()
    selected_data = bytearray(original)
    selected_data[ELF_PADDING_OFFSET] ^= 0xFF
    before_path = tmp_path / "before.elf"
    selected_path = tmp_path / "selected.elf"
    before_path.write_bytes(original)
    selected_path.write_bytes(selected_data)

    with pytest.raises(ValueError, match="no 1-byte loadable-section witness"):
        elf_memory_witness(before_path, selected_path, size=1)
