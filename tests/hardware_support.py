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
    runner_args = list(profile.runner_args)
    if profile.probe_serial and not any(item.startswith("--serial") for item in runner_args):
        runner_args.append(f"--serial={profile.probe_serial}")
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
        "runner_args": runner_args,
        "debug_runner_args": runner_args,
        "rtt_runner_args": runner_args,
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
    if profile.flash is not None:
        record.update(
            precondition_build_dir=str(build_dir.parent / profile.flash.precondition_build),
            quiescence_timeout=profile.flash.quiescence_timeout,
        )
    if profile.debug is not None:
        record["debug_breakpoint"] = profile.debug.breakpoint
    if profile.rtt is not None:
        record.update(
            rtt_port=profile.rtt.port,
            expected_rtt_response=profile.rtt.response,
            rtt_input=profile.rtt.input,
            rtt_timeout=profile.rtt.timeout,
            rtt_program_survives_reset=profile.rtt.program_survives_reset,
        )
    if profile.semihosting is not None:
        record.update(
            semihosting_commands=list(profile.semihosting.commands),
            semihosting_gdb_init=list(profile.semihosting.gdb_commands),
            expected_output=profile.semihosting.output,
            timeout=profile.semihosting.timeout,
        )
    return record


class HardwarePreparation:
    """Lazily prepare selected profiles, caching shared recipes per pytest session."""

    def __init__(self, inventory: Inventory, build_root: Path, config_root: Path):
        self.inventory = inventory
        self.build_root = build_root
        self.config_root = config_root
        self.built: set[tuple[str, str]] = set()

    def prepare(self, identifier: str) -> dict[str, Any]:
        target_id, profile_name = identifier.split(":", 1)
        target = self.inventory.target(target_id)
        profile = next(item for item in target.profiles if item.name == profile_name)
        if not target.zephyr_base.is_dir():
            pytest.fail(f"target {target.id} Zephyr tree is missing: {target.zephyr_base}")
        if not target.west.is_file() or not os.access(target.west, os.X_OK):
            pytest.fail(f"target {target.id} west executable is unavailable: {target.west}")
        host = self.inventory.host(target.host)
        config_path = self.config_root / f"{host.id}.yaml"
        config_path.write_text(render_product_config(host))
        build_dir = self._prepare_build(target, profile.build, config_path)
        if profile.flash is not None:
            self._prepare_build(target, profile.flash.precondition_build, config_path)
        return _record(target, host, profile, build_dir, config_path)

    def _prepare_build(self, target: InventoryTarget, build_name: str, config_path: Path) -> Path:
        build_dir = self.build_root / target.id / build_name
        build_dir.parent.mkdir(exist_ok=True)
        build_key = (target.id, build_name)
        if build_key not in self.built:
            recipe = target.build(build_name)
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
            environment = os.environ.copy()
            for name in (
                "ZEPHYR_REMOTE_OPENOCD_REMOTE",
                "ZEPHYR_REMOTE_OPENOCD_RECORD",
                "ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION",
            ):
                environment.pop(name, None)
            environment.update(
                EXTRA_ZEPHYR_MODULES=str(ROOT),
                ZEPHYR_REMOTE_OPENOCD_CONFIG=str(config_path),
            )
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
            self.built.add(build_key)
        return build_dir


@pytest.fixture(scope="session")
def prepared_hardware(hardware_inventory: Inventory, tmp_path_factory: pytest.TempPathFactory):
    return HardwarePreparation(
        hardware_inventory,
        tmp_path_factory.mktemp("hardware_builds"),
        tmp_path_factory.mktemp("hardware_config"),
    )


def records_for(records: list[dict[str, Any]], capability: str) -> list[dict[str, Any]]:
    """Return independent profile records advertising one capability."""
    return [record for record in records if capability in _capabilities(record)]


def _capabilities(record: dict[str, Any]) -> set[str]:
    return set(record["capabilities"])
