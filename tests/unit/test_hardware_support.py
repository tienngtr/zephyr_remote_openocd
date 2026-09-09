# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import struct
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from elftools.elf.elffile import ELFFile
from zephyr_remote_openocd.config import load_config, resolve_remote

from tests.hardware_support import HardwarePreparation, _record, elf_memory_witness, records_for
from tests.inventory import load_inventory
from tests.support import ROOT


def test_inventory_profiles_become_independent_capability_records(tmp_path):
    inventory = load_inventory(ROOT / "tests/fixtures/hardware.example.toml")
    host = inventory.host("lab")
    target = inventory.target("board")
    profile = target.profile("debug")
    record = _record(target, host, profile, tmp_path / "build", tmp_path / "config.yaml")
    assert {"debug", "attach", "debugserver"}.issubset(record["capabilities"])
    assert records_for([record], "rtt") == []
    assert records_for([record], "debug") == [record]
    assert record["debug_breakpoint"] == "main"
    assert record["attach_precondition_build_dir"] == str(tmp_path / "minimal")
    assert "serial_device" not in record


def test_probe_serial_is_translated_to_runner_argument(tmp_path):
    inventory = load_inventory(ROOT / "tests/fixtures/hardware.example.toml")
    host = inventory.host("lab")
    target = inventory.target("board")
    profile = replace(
        target.profile("flash"),
        probe_serial="example_probe",
    )
    record = _record(target, host, profile, tmp_path / "build", tmp_path / "config.yaml")
    assert "--serial=example_probe" in record["runner_args"]
    assert record["precondition_build_dir"] == str(tmp_path / "minimal")
    assert record["quiescence_timeout"] == 2


def test_preparation_builds_only_requested_recipes_and_caches_success(tmp_path, monkeypatch):
    inventory = load_inventory(ROOT / "tests/fixtures/hardware.example.toml")
    original = inventory.target("board")
    target = replace(original, zephyr_base=tmp_path, west=Path(sys.executable))
    # An unavailable unrelated target and recipe must not affect selection.
    unrelated = replace(original, id="unavailable")
    extra = replace(target.builds[0], name="unused", application="/unavailable/application")
    unused_profile = replace(target.profile("flash"), name="unused", build="unused")
    target = replace(
        target, builds=(*target.builds, extra), profiles=(*target.profiles, unused_profile)
    )
    inventory = replace(inventory, targets=(unrelated, target))
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


def test_elf_memory_witness_finds_bytes_that_distinguish_images(tmp_path: Path) -> None:
    original = Path(sys.executable).read_bytes()
    selected_data = bytearray(original)
    with Path(sys.executable).open("rb") as stream:
        elf = ELFFile(stream)
        segment_index, segment, section = next(
            (segment_index, segment, section)
            for segment_index, segment in enumerate(elf.iter_segments())
            if segment["p_type"] == "PT_LOAD"
            for section in elf.iter_sections()
            if int(section["sh_flags"]) & 0x2
            and section["sh_type"] != "SHT_NOBITS"
            and int(section["sh_size"]) >= 16
            and int(segment["p_vaddr"]) < int(section["sh_addr"])
            and int(section["sh_addr"]) + int(section["sh_size"])
            < int(segment["p_vaddr"]) + int(segment["p_filesz"])
            and int(segment["p_offset"]) <= int(section["sh_offset"])
            and int(section["sh_offset"]) + int(section["sh_size"])
            <= int(segment["p_offset"]) + int(segment["p_filesz"])
        )
        selected_data[int(section["sh_offset"])] ^= 0xFF
        original_vma = int(segment["p_vaddr"])
        header_offset = int(elf.header["e_phoff"]) + segment_index * int(elf.header["e_phentsize"])
        vma_offset = header_offset + (16 if elf.elfclass == 64 else 8)
        byte_order = "<" if elf.little_endian else ">"
        address_format = "Q" if elf.elfclass == 64 else "I"
        struct.pack_into(byte_order + address_format, selected_data, vma_offset, original_vma + 1)
        expected_address = (
            int(segment["p_paddr"]) + int(section["sh_offset"]) - int(segment["p_offset"])
        )
    before_path = tmp_path / "before.elf"
    selected_path = tmp_path / "selected.elf"
    before_path.write_bytes(original)
    selected_path.write_bytes(selected_data)

    address, before, selected = elf_memory_witness(before_path, selected_path)
    assert address == expected_address
    assert len(before) == len(selected) == 16
    assert before != selected


def test_elf_memory_witness_ignores_load_segment_padding(tmp_path: Path) -> None:
    original = Path(sys.executable).read_bytes()
    selected_data = bytearray(original)
    with Path(sys.executable).open("rb") as stream:
        elf = ELFFile(stream)
        occupied = [
            (int(section["sh_offset"]), int(section["sh_offset"]) + int(section["sh_size"]))
            for section in elf.iter_sections()
            if int(section["sh_flags"]) & 0x2 and section["sh_type"] != "SHT_NOBITS"
        ]
        padding_offset = next(
            offset
            for segment in elf.iter_segments()
            if segment["p_type"] == "PT_LOAD" and int(segment["p_offset"]) > 0
            for offset in range(
                int(segment["p_offset"]), int(segment["p_offset"]) + int(segment["p_filesz"])
            )
            if all(
                offset + 1 <= section_start or offset >= section_end
                for section_start, section_end in occupied
            )
        )
        selected_data[padding_offset] ^= 0xFF
    before_path = tmp_path / "before.elf"
    selected_path = tmp_path / "selected.elf"
    before_path.write_bytes(original)
    selected_path.write_bytes(selected_data)

    with pytest.raises(ValueError, match="no 1-byte loadable-section witness"):
        elf_memory_witness(before_path, selected_path, size=1)
