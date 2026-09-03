# SPDX-License-Identifier: Apache-2.0

"""Pytest fixtures and adapters for inventory-selected external tests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.inventory import (
    Inventory,
    InventoryHost,
    InventoryTarget,
    OperationProfile,
    render_product_config,
)
from tests.support import ROOT


def _record(
    target: InventoryTarget,
    host: InventoryHost,
    profile: OperationProfile,
    build_dir: Path,
    config_path: Path,
) -> dict[str, Any]:
    endpoint = target.endpoint(profile.serial) if profile.serial else None
    record: dict[str, Any] = {
        "id": f"{target.id}:{profile.name}",
        "target_id": target.id,
        "profile": profile.name,
        "capabilities": list(profile.capabilities),
        "host": host.address,
        "ssh_command": list(host.ssh_command),
        "build_dir": str(build_dir),
        "config_path": str(config_path),
        "west": str(target.west),
        "workspace": str(target.zephyr_base.parent),
        "elf_file": str(build_dir / "zephyr" / "zephyr.elf"),
        "hex_file": str(build_dir / "zephyr" / "zephyr.hex"),
        "bin_file": str(build_dir / "zephyr" / "zephyr.bin"),
        "thread_build_dir": str(build_dir),
        "rtt_build_dir": str(build_dir),
        "rtt_elf_file": str(build_dir / "zephyr" / "zephyr.elf"),
        "gdb_client_port": 3333,
        "enabled_local_ports": (6333, 4444),
        "gdb": str(target.gdb) if target.gdb else None,
        "runner_args": list(profile.runner_args),
        "debug_runner_args": list(profile.runner_args),
        "rtt_runner_args": list(profile.runner_args),
        "environment": dict(profile.environment),
        "expected_flash_patterns": list(profile.expectations.patterns),
        "debug_patterns": list(profile.expectations.patterns),
        "supports_thread_info": "thread_info" in profile.capabilities,
        "supports_rtt": "rtt" in profile.capabilities,
        "supports_semihosting": "semihosting" in profile.capabilities,
        "assert_openocd_bindto": profile.expectations.assert_bindto,
    }
    if endpoint is not None:
        record.update(
            serial_device=endpoint.device,
            serial_baud=endpoint.baud,
            serial_timeout=endpoint.timeout,
            expected_pattern=endpoint.pattern,
            serial_data_bits=endpoint.data_bits,
            serial_parity=endpoint.parity,
            serial_stop_bits=endpoint.stop_bits,
            serial_flow_control=endpoint.flow_control,
        )
    if profile.expectations.thread_info_pattern:
        record["thread_info_pattern"] = profile.expectations.thread_info_pattern
    if profile.rtt is not None:
        record.update(
            rtt_port=profile.rtt.port,
            expected_rtt_response=profile.rtt.response,
            rtt_input=profile.rtt.input,
            rtt_timeout=profile.rtt.timeout,
        )
    if profile.semihosting is not None:
        record.update(
            semihosting_commands=list(profile.semihosting.commands),
            semihosting_gdb_init=list(profile.semihosting.gdb_commands),
            expected_output=profile.semihosting.output,
            timeout=profile.semihosting.timeout,
            normal_gdb_init=list(profile.semihosting.gdb_commands),
            interrupt_gdb_init=list(profile.semihosting.gdb_commands),
        )
    return record


@pytest.fixture(scope="session")
def prepared_hardware(hardware_inventory: Inventory, tmp_path_factory: pytest.TempPathFactory):
    """Build each referenced recipe once and expose profile test records."""
    build_root = tmp_path_factory.mktemp("hardware-builds")
    config_root = tmp_path_factory.mktemp("hardware-config")
    config_paths: dict[str, Path] = {}
    for host in hardware_inventory.hosts:
        config_path = config_root / f"{host.id}.toml"
        config_path.write_text(render_product_config(host))
        config_paths[host.id] = config_path

    records: list[dict[str, Any]] = []
    built: set[tuple[str, str]] = set()
    environment = os.environ.copy()
    environment.pop("ZEPHYR_REMOTE_OPENOCD_RECORD", None)
    environment["EXTRA_ZEPHYR_MODULES"] = str(ROOT)
    for target in hardware_inventory.targets:
        host = hardware_inventory.host(target.host)
        target_root = build_root / target.id
        target_root.mkdir()
        for profile in target.profiles:
            build_key = (target.id, profile.build)
            build_dir = target_root / profile.build
            recipe = target.build(profile.build)
            if build_key not in built:
                application = Path(recipe.application)
                if not application.is_absolute():
                    application = target.zephyr_base / application
                command = [
                    str(target.west),
                    "build",
                    "-b",
                    recipe.board,
                    str(application),
                    "-d",
                    str(build_dir),
                    *recipe.west_args,
                ]
                if recipe.cmake_args:
                    command.extend(("--", *recipe.cmake_args))
                result = subprocess.run(
                    command,
                    cwd=target.zephyr_base.parent,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=600,
                )
                if result.returncode:
                    pytest.fail(f"build recipe {target.id}:{recipe.name} failed:\n{result.stdout}")
                built.add(build_key)
            records.append(_record(target, host, profile, build_dir, config_paths[target.host]))
    return records


def records_for(records: list[dict[str, Any]], capability: str) -> list[dict[str, Any]]:
    """Return independent profile records advertising one capability."""
    return [record for record in records if capability in _capabilities(record)]


def _capabilities(record: dict[str, Any]) -> set[str]:
    return set(record["capabilities"])
