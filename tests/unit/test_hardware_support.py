# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import struct
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
from tests.inventory import load_inventory
from tests.inventory_samples import inventory_document


def test_preparation_builds_only_requested_recipes_and_caches_success(tmp_path, monkeypatch):
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
    preparation = HardwarePreparation(inventory, build_root, config_root)
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "developer_remote")
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", "/developer/config.yaml")
    with patch("tests.hardware_support.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=1, stdout="build failed")
        with pytest.raises(pytest.fail.Exception, match="build failed"):
            preparation.prepare("target:profile", "flash")
        run.return_value = SimpleNamespace(returncode=0, stdout="")
        flash = preparation.prepare("target:profile", "flash")
        debug = preparation.prepare("target:profile", "debug")
    # Failed attempts are retried; the successful flash preparation builds both
    # the intended and precondition recipes, and debug reuses the intended one.
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
    assert not (build_root / "unavailable").exists()
    assert not (build_root / "target" / "unused").exists()


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
