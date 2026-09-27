# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import struct
import subprocess
import sys
from copy import deepcopy
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
    hardware_cache_root,
    hardware_shared_cache_root,
)
from tests.inventory import Inventory, load_inventory, render_product_config
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
    with patch("tests.hardware_support.subprocess.run", autospec=True) as run:
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
    assert all("--" in command for command in commands)
    assert all(
        any(argument.startswith("-DUSER_CACHE_DIR=") for argument in command)
        for command in commands
    )


def test_preparation_preserves_typed_user_cache_dir(tmp_path: Path) -> None:
    preparation, inventory, build_root = _preparation_with_unavailable_recipe(tmp_path)
    original_target = inventory.target("target")
    typed_cache = "-DUSER_CACHE_DIR:PATH=/inventory/cache"
    builds = tuple(
        replace(recipe, cmake_args=(typed_cache,)) if recipe.name == "application" else recipe
        for recipe in original_target.builds
    )
    target = replace(original_target, builds=builds)
    inventory = replace(
        inventory,
        targets=tuple(target if item.name == target.name else item for item in inventory.targets),
    )
    preparation = HardwarePreparation(inventory, build_root, tmp_path / "configs")

    with patch("tests.hardware_support.subprocess.run", autospec=True) as run:
        run.return_value = subprocess.CompletedProcess([], 0, "")
        preparation.prepare("target:profile", "debug")

    command = run.call_args.args[0]
    definitions = [argument for argument in command if argument.startswith("-DUSER_CACHE_DIR")]
    assert definitions == [typed_cache]


def test_preparation_retries_failed_build_and_caches_success(tmp_path, monkeypatch):
    preparation, inventory, build_root = _preparation_with_unavailable_recipe(tmp_path)
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "developer_remote")
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", "/developer/config.yaml")
    with patch("tests.hardware_support.subprocess.run", autospec=True) as run:
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "build failed"),
            subprocess.CompletedProcess([], 0, ""),
            subprocess.CompletedProcess([], 0, ""),
        ]
        with pytest.raises(pytest.fail.Exception):
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
    environment = run.call_args.kwargs["env"]
    assert "ZEPHYR_REMOTE_OPENOCD_REMOTE" not in environment
    assert environment["ZEPHYR_REMOTE_OPENOCD_CONFIG"] == str(flash.target.config_path)
    selected = resolve_remote(load_config(flash.target.config_path), remote_name="host")
    assert selected.ssh_host == inventory.host("host").ssh_host


def test_hardware_cache_root_uses_build_inputs_and_checkout(tmp_path: Path) -> None:
    first_path = tmp_path / "first.yaml"
    document = inventory_document()
    first_path.write_text(yaml.safe_dump(document, sort_keys=False))
    first = load_inventory(first_path)

    unrelated_document = deepcopy(document)
    unrelated_document["targets"]["target"]["serial"]["console"]["device"] = "/dev/other"
    second_path = tmp_path / "second.yaml"
    second_path.write_text(yaml.safe_dump(unrelated_document, sort_keys=False))
    second = load_inventory(second_path)
    assert hardware_cache_root(first, repository_root=tmp_path / "checkout") == (
        hardware_cache_root(second, repository_root=tmp_path / "checkout")
    )

    build_document = deepcopy(document)
    build_document["targets"]["target"]["builds"]["application"]["cmake_args"] = [
        "-DCONFIG_ASSERT=y"
    ]
    build_path = tmp_path / "build.yaml"
    build_path.write_text(yaml.safe_dump(build_document, sort_keys=False))
    changed_build = load_inventory(build_path)
    assert hardware_cache_root(first, repository_root=tmp_path / "checkout") != (
        hardware_cache_root(changed_build, repository_root=tmp_path / "checkout")
    )
    assert hardware_cache_root(first, repository_root=tmp_path / "checkout") != (
        hardware_cache_root(first, repository_root=tmp_path / "other-checkout")
    )


def test_configured_build_environment_affects_cache_identity(tmp_path: Path) -> None:
    first_document = inventory_document()
    first_document["build_environments"]["environment"]["environment"] = {
        "ZEPHYR_TOOLCHAIN_VARIANT": "zephyr",
        "ZEPHYR_SDK_INSTALL_DIR": "/opt/zephyr-sdk-a",
    }
    first_path = tmp_path / "first.yaml"
    first_path.write_text(yaml.safe_dump(first_document, sort_keys=False))
    first = load_inventory(first_path)

    second_document = deepcopy(first_document)
    second_document["build_environments"]["environment"]["environment"][
        "ZEPHYR_SDK_INSTALL_DIR"
    ] = "/opt/zephyr-sdk-b"
    second_path = tmp_path / "second.yaml"
    second_path.write_text(yaml.safe_dump(second_document, sort_keys=False))
    second = load_inventory(second_path)

    assert hardware_cache_root(first, repository_root=tmp_path / "checkout") != (
        hardware_cache_root(second, repository_root=tmp_path / "checkout")
    )
    assert hardware_shared_cache_root(
        first.build_environment("environment"), repository_root=tmp_path / "checkout"
    ) != hardware_shared_cache_root(
        second.build_environment("environment"), repository_root=tmp_path / "checkout"
    )


def test_preparation_uses_only_configured_build_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = inventory_document(zephyr_base=str(tmp_path), west=sys.executable)
    document["build_environments"]["environment"]["environment"] = {
        "ZEPHYR_TOOLCHAIN_VARIANT": "zephyr",
        "ZEPHYR_SDK_INSTALL_DIR": "/opt/zephyr-sdk-a",
    }
    inventory_path = tmp_path / "hardware.yaml"
    inventory_path.write_text(yaml.safe_dump(document, sort_keys=False))
    inventory = load_inventory(inventory_path)

    monkeypatch.setenv("ZEPHYR_TOOLCHAIN_VARIANT", "ambient")
    monkeypatch.setenv("CMAKE_PREFIX_PATH", "/ambient/cmake-prefix")
    preparation = HardwarePreparation(
        inventory,
        tmp_path / "builds",
        tmp_path / "configs",
        cache_root=tmp_path / "cache",
    )
    with patch("tests.hardware_support.subprocess.run", autospec=True) as run:
        run.return_value = subprocess.CompletedProcess([], 0, "")
        preparation.prepare("target:profile", "debug")

    environment = run.call_args.kwargs["env"]
    assert environment["ZEPHYR_TOOLCHAIN_VARIANT"] == "zephyr"
    assert environment["ZEPHYR_SDK_INSTALL_DIR"] == "/opt/zephyr-sdk-a"
    assert "CMAKE_PREFIX_PATH" not in environment


def test_shared_hardware_cache_uses_build_environment_and_checkout(tmp_path: Path) -> None:
    inventory_path = tmp_path / "hardware.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory_document(), sort_keys=False))
    inventory = load_inventory(inventory_path)
    environment = inventory.build_environment("environment")

    first = hardware_shared_cache_root(environment, repository_root=tmp_path / "checkout")
    assert first == hardware_shared_cache_root(environment, repository_root=tmp_path / "checkout")
    assert first != hardware_shared_cache_root(
        replace(environment, zephyr_base=tmp_path / "other-zephyr"),
        repository_root=tmp_path / "checkout",
    )
    assert first != hardware_shared_cache_root(
        environment, repository_root=tmp_path / "other-checkout"
    )


def test_preparation_reuses_warm_build_with_redirected_caches(tmp_path: Path) -> None:
    preparation, _inventory, build_root = _preparation_with_unavailable_recipe(tmp_path)
    cache_root = tmp_path / "cache"
    preparation = HardwarePreparation(
        preparation.inventory,
        build_root,
        tmp_path / "configs",
        cache_root=cache_root,
    )
    elf = build_root / "target" / "application" / "zephyr" / "zephyr.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"existing build")
    (elf.parents[1] / "CMakeCache.txt").write_text("cached")
    config_path = tmp_path / "configs" / "host.yaml"
    config_path.write_text(render_product_config(preparation.inventory.host("host")))
    unchanged_timestamp = 1_000_000_000_000_000_000
    os.utime(config_path, ns=(unchanged_timestamp, unchanged_timestamp))

    with patch("tests.hardware_support.subprocess.run", autospec=True) as run:
        run.return_value = subprocess.CompletedProcess([], 0, "")
        preparation.prepare("target:profile", "debug")

    command = run.call_args.args[0]
    assert command[command.index("-d") + 1] == str(build_root / "target" / "application")
    assert "--pristine=never" in command
    assert "--" not in command
    assert not any(argument.startswith("-DUSER_CACHE_DIR=") for argument in command)
    environment = run.call_args.kwargs["env"]
    assert environment["CCACHE_DIR"] == str(cache_root / "ccache")
    assert environment["CCACHE_TEMPDIR"] == str(cache_root / "ccache-tmp")
    assert preparation.build_timings[-1].cache_state == "warm"
    assert config_path.stat().st_mtime_ns == unchanged_timestamp


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

    with pytest.raises(ValueError, match="loadable-section witness"):
        elf_memory_witness(before_path, selected_path, size=1)
