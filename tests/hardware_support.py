# SPDX-License-Identifier: Apache-2.0

"""Typed preparation support for inventory-selected external tests."""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from tests.inventory import (
    AttachOperation,
    BuildEnvironment,
    DebugOperation,
    DebugServerOperation,
    FlashOperation,
    Inventory,
    InventoryHost,
    InventoryTarget,
    Operation,
    OperationProfile,
    RttOperation,
    SemihostingOperation,
    SerialEndpoint,
    ThreadInfoOperation,
    Toolchain,
    render_product_config,
)
from tests.support import ROOT


def free_loopback_ports(count: int) -> tuple[int, ...]:
    """Allocate distinct ephemeral loopback ports for test endpoints."""
    listeners = []
    try:
        for _ in range(count):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        return tuple(listener.getsockname()[1] for listener in listeners)
    finally:
        for listener in listeners:
            listener.close()


def elf_memory_witness(
    precondition_elf: Path | str, selected_elf: Path | str, *, size: int = 16
) -> tuple[int, bytes, bytes]:
    """Find loadable-section bytes that distinguish two ELF files."""

    def loadable_sections(path_value: Path | str) -> list[tuple[bool, int, bytes]]:
        path = Path(path_value)
        with path.open("rb") as stream:
            elf = ELFFile(stream)
            segments = [
                segment for segment in elf.iter_segments() if segment["p_type"] == "PT_LOAD"
            ]
            candidates = []
            for section in elf.iter_sections():
                section_size = int(section["sh_size"])
                section_flags = int(section["sh_flags"])
                if (
                    not section_flags & 0x2
                    or section["sh_type"] == "SHT_NOBITS"
                    or section_size < size
                ):
                    continue
                section_vma = int(section["sh_addr"])
                section_offset = int(section["sh_offset"])
                for segment in segments:
                    segment_vma = int(segment["p_vaddr"])
                    segment_offset = int(segment["p_offset"])
                    if (
                        segment_vma <= section_vma
                        and section_vma + section_size <= segment_vma + int(segment["p_filesz"])
                        and segment_offset <= section_offset
                        and section_offset + section_size
                        <= segment_offset + int(segment["p_filesz"])
                    ):
                        load_address = int(segment["p_paddr"]) + section_offset - segment_offset
                        candidates.append((not section_flags & 0x1, load_address, section.data()))
                        break
            return sorted(candidates, key=lambda candidate: not candidate[0])

    before_sections = loadable_sections(precondition_elf)
    selected_sections = loadable_sections(selected_elf)
    for _, before_address, before_data in before_sections:
        before_end = before_address + len(before_data)
        for _, selected_address, selected_data in selected_sections:
            start = max(before_address, selected_address)
            end = min(before_end, selected_address + len(selected_data))
            if end - start < size:
                continue
            before_offset = start - before_address
            selected_offset = start - selected_address
            for offset in range(end - start - size + 1):
                before = before_data[before_offset + offset : before_offset + offset + size]
                selected = selected_data[selected_offset + offset : selected_offset + offset + size]
                if before != selected:
                    return start + offset, before, selected
    raise ValueError(
        f"no {size}-byte loadable-section witness distinguishes "
        f"{precondition_elf} from {selected_elf}"
    )


@dataclass(frozen=True)
class PreparedTarget:
    """Common resolved state shared by every operation fixture."""

    id: str
    target_name: str
    profile_name: str
    host: InventoryHost
    build_environment: BuildEnvironment
    toolchain: Toolchain | None
    build_dir: Path
    config_path: Path
    runner_args: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]

    @property
    def workspace(self) -> Path:
        return self.build_environment.zephyr_base.parent

    @property
    def elf_file(self) -> Path:
        return self.build_dir / "zephyr" / "zephyr.elf"

    @property
    def gdb(self) -> Path:
        if self.toolchain is None:
            raise ValueError(f"prepared target {self.id} has no toolchain")
        return self.toolchain.gdb


@dataclass(frozen=True)
class FlashFixture:
    target: PreparedTarget
    operation: FlashOperation
    serial: SerialEndpoint
    precondition_build_dir: Path


@dataclass(frozen=True)
class DebugFixture:
    target: PreparedTarget
    operation: DebugOperation


@dataclass(frozen=True)
class AttachFixture:
    target: PreparedTarget
    operation: AttachOperation
    precondition_build_dir: Path

    @property
    def precondition_elf_file(self) -> Path:
        return self.precondition_build_dir / "zephyr" / "zephyr.elf"


@dataclass(frozen=True)
class DebugServerFixture:
    target: PreparedTarget
    operation: DebugServerOperation


@dataclass(frozen=True)
class ThreadInfoFixture:
    target: PreparedTarget
    operation: ThreadInfoOperation


@dataclass(frozen=True)
class RttFixture:
    target: PreparedTarget
    operation: RttOperation


@dataclass(frozen=True)
class SemihostingFixture:
    target: PreparedTarget
    operation: SemihostingOperation


type PreparedOperation = (
    FlashFixture
    | DebugFixture
    | AttachFixture
    | DebugServerFixture
    | ThreadInfoFixture
    | RttFixture
    | SemihostingFixture
)


class HardwarePreparation:
    """Lazily prepare selected operations, caching builds per pytest session."""

    def __init__(self, inventory: Inventory, build_root: Path, config_root: Path):
        self.inventory = inventory
        self.build_root = build_root
        self.config_root = config_root
        self.built: set[tuple[str, str]] = set()

    def prepare(self, identifier: str, operation_name: str) -> PreparedOperation:
        target_name, profile_name = identifier.split(":", 1)
        target = self.inventory.target(target_name)
        profile = target.profile(profile_name)
        operation = profile.operation(operation_name)
        build_environment = self.inventory.build_environment(target.build_environment)
        self._check_build_environment(target, build_environment)
        host = self.inventory.host(target.host)
        toolchain = self.inventory.toolchain(target.toolchain) if target.toolchain else None
        config_path = self.config_root / f"{host.name}.yaml"
        config_path.write_text(render_product_config(host), encoding="utf-8")
        build_dir = self._prepare_build(target, build_environment, profile.build, config_path)
        prepared = self._prepared_target(
            target, profile, host, build_environment, toolchain, build_dir, config_path
        )
        return self._prepare_operation(prepared, target, profile, operation, config_path)

    @staticmethod
    def _check_build_environment(target: InventoryTarget, environment: BuildEnvironment) -> None:
        if not environment.zephyr_base.is_dir():
            pytest.fail(f"target {target.name} Zephyr tree is missing: {environment.zephyr_base}")
        if not environment.west.is_file() or not os.access(environment.west, os.X_OK):
            pytest.fail(f"target {target.name} west executable is unavailable: {environment.west}")

    def _prepare_operation(
        self,
        prepared: PreparedTarget,
        target: InventoryTarget,
        profile: OperationProfile,
        operation: Operation,
        config_path: Path,
    ) -> PreparedOperation:
        if isinstance(operation, FlashOperation):
            precondition = self._prepare_build(
                target, prepared.build_environment, operation.precondition_build, config_path
            )
            return FlashFixture(
                prepared, operation, target.endpoint(operation.serial.endpoint), precondition
            )
        if isinstance(operation, DebugOperation):
            return DebugFixture(prepared, operation)
        if isinstance(operation, AttachOperation):
            precondition = self._prepare_build(
                target, prepared.build_environment, operation.precondition_build, config_path
            )
            return AttachFixture(prepared, operation, precondition)
        if isinstance(operation, DebugServerOperation):
            return DebugServerFixture(prepared, operation)
        if isinstance(operation, ThreadInfoOperation):
            return ThreadInfoFixture(prepared, operation)
        if isinstance(operation, RttOperation):
            return RttFixture(prepared, operation)
        if isinstance(operation, SemihostingOperation):
            return SemihostingFixture(prepared, operation)
        raise AssertionError(f"unsupported operation {type(operation).__name__}: {operation!r}")

    @staticmethod
    def _prepared_target(
        target: InventoryTarget,
        profile: OperationProfile,
        host: InventoryHost,
        build_environment: BuildEnvironment,
        toolchain: Toolchain | None,
        build_dir: Path,
        config_path: Path,
    ) -> PreparedTarget:
        runner_args = list(profile.runner_args)
        if profile.probe_serial and not any(item.startswith("--serial") for item in runner_args):
            runner_args.append(f"--serial={profile.probe_serial}")
        return PreparedTarget(
            f"{target.name}:{profile.name}",
            target.name,
            profile.name,
            host,
            build_environment,
            toolchain,
            build_dir,
            config_path,
            tuple(runner_args),
            profile.environment,
        )

    def _prepare_build(
        self,
        target: InventoryTarget,
        build_environment: BuildEnvironment,
        build_name: str,
        config_path: Path,
    ) -> Path:
        build_dir = self.build_root / target.name / build_name
        build_dir.parent.mkdir(exist_ok=True)
        build_key = (target.name, build_name)
        if build_key not in self.built:
            recipe = target.build(build_name)
            application = Path(recipe.application)
            if not application.is_absolute():
                application = build_environment.zephyr_base / application
            command = [
                str(build_environment.west),
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
                "ZRO_RECORD",
                "ZRO_RECORD_OPENOCD_VERSION",
            ):
                environment.pop(name, None)
            environment.update(
                EXTRA_ZEPHYR_MODULES=str(ROOT),
                ZEPHYR_REMOTE_OPENOCD_CONFIG=str(config_path),
            )
            result = subprocess.run(
                command,
                cwd=build_environment.zephyr_base.parent,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=600,
            )
            if result.returncode:
                pytest.fail(f"build recipe {target.name}:{recipe.name} failed:\n{result.stdout}")
            self.built.add(build_key)
        return build_dir


@pytest.fixture(scope="session")
def prepared_hardware(hardware_inventory: Inventory, tmp_path_factory: pytest.TempPathFactory):
    return HardwarePreparation(
        hardware_inventory,
        tmp_path_factory.mktemp("hardware_builds"),
        tmp_path_factory.mktemp("hardware_inventory"),
    )
