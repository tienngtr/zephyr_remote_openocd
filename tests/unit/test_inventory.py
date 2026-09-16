# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from zephyr_remote_openocd.config import load_config, resolve_remote

from tests.inventory import (
    InventoryError,
    load_inventory,
    render_product_config,
)
from tests.inventory_samples import inventory_document
from tests.support import ROOT

STARTER_EXAMPLE = ROOT / "tests/fixtures/hardware.example.yaml"
EXAMPLE = ROOT / "tests/fixtures/hardware.complete.example.yaml"
DELETE = object()


@pytest.mark.parametrize("path", (STARTER_EXAMPLE, EXAMPLE))
def test_hardware_example_loads(path: Path) -> None:
    load_inventory(path)


def write_inventory(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "hardware.yaml"
    text = document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def change(document: dict, path: tuple[str, ...], value: object) -> dict:
    updated = copy.deepcopy(document)
    table = updated
    for key in path[:-1]:
        table = table[key]
    if value is DELETE:
        del table[path[-1]]
    else:
        table[path[-1]] = value
    return updated


def test_inventory_host_is_renderable_as_product_config(tmp_path: Path) -> None:
    inventory = load_inventory(write_inventory(tmp_path, inventory_document()))
    host = inventory.hosts[0]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(render_product_config(host), encoding="utf-8")
    assert load_config(config_path).default_runner == "openocd"


@pytest.mark.parametrize("name", ("lab", "on", "off", "true", "null"))
def test_rendered_inventory_round_trips_through_product_schema(tmp_path, name):
    document = inventory_document()
    document["hosts"][name] = document["hosts"].pop("host")
    document["targets"]["target"]["host"] = name
    inventory = load_inventory(write_inventory(tmp_path, document))
    host = inventory.host(name)
    path = tmp_path / "config.yaml"
    path.write_text(render_product_config(host, default_runner="remote_openocd"))
    selected = resolve_remote(load_config(path))
    assert selected.name == name
    assert selected.ssh_host == host.ssh_host
    assert selected.openocd_command == host.openocd_command
    assert selected.ssh_command == host.ssh_command
    assert selected.forward_env == host.forward_env
    assert [(item.local, item.remote) for item in selected.path_mappings] == [
        (item.local, item.remote) for item in host.path_mappings
    ]


@pytest.mark.parametrize(
    ("path", "value", "diagnostic"),
    (
        (("future",), True, "future"),
        (("hosts", "host", "ssh_host"), DELETE, "ssh_host"),
        (("hosts", "host", "openocd_command"), ["openocd"], "openocd_command"),
        (("hosts", "host", "openocd_command"), ["/"], "openocd_command"),
        (("hosts", "host", "ssh_command"), [""], "ssh_command"),
        (
            ("targets", "target", "profiles", "profile", "capabilities"),
            ["debug"],
            "capabilities",
        ),
        (
            (
                "targets",
                "target",
                "profiles",
                "profile",
                "operations",
                "debug",
                "breakpoint",
            ),
            "main + 4",
            "breakpoint",
        ),
        (
            (
                "targets",
                "target",
                "profiles",
                "profile",
                "operations",
                "flash",
                "serial",
            ),
            DELETE,
            "serial",
        ),
        (
            (
                "targets",
                "target",
                "profiles",
                "rtt_profile",
                "operations",
                "rtt",
                "program_survives_reset",
            ),
            False,
            "program_survives_reset",
        ),
    ),
)
def test_schema_rejects_invalid_structure(tmp_path, path, value, diagnostic) -> None:
    document = change(inventory_document(), path, value)
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, document))


@pytest.mark.parametrize(
    ("path", "value", "diagnostic"),
    (
        (("targets", "target", "host"), "missing", "unknown host"),
        (
            ("targets", "target", "build_environment"),
            "missing",
            "unknown build environment",
        ),
        (("targets", "target", "toolchain"), "missing", "unknown toolchain"),
        (
            ("targets", "target", "profiles", "profile", "build"),
            "missing",
            "unknown build",
        ),
        (
            ("targets", "target", "profiles", "profile", "environment"),
            {"OTHER": "1"},
            "allow-list",
        ),
        (
            (
                "targets",
                "target",
                "profiles",
                "profile",
                "operations",
                "flash",
                "precondition_build",
            ),
            "application",
            "must differ",
        ),
        (
            (
                "targets",
                "target",
                "profiles",
                "profile",
                "operations",
                "flash",
                "precondition_build",
            ),
            "missing",
            "unknown build",
        ),
        (
            (
                "targets",
                "target",
                "profiles",
                "profile",
                "operations",
                "flash",
                "serial",
                "endpoint",
            ),
            "missing",
            "unknown serial",
        ),
        (
            ("targets", "target", "builds", "application", "application"),
            "../escape",
            "escape",
        ),
    ),
)
def test_semantic_references_are_validated(tmp_path, path, value, diagnostic) -> None:
    document = change(inventory_document(), path, value)
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, document))


def test_direct_gdb_operations_require_a_toolchain(tmp_path: Path) -> None:
    document = change(inventory_document(), ("targets", "target", "toolchain"), DELETE)
    with pytest.raises(InventoryError, match="toolchain.*required"):
        load_inventory(write_inventory(tmp_path, document))


@pytest.mark.parametrize(
    ("text", "diagnostic"),
    (
        ("", "one YAML document"),
        ("null\n", "YAML mapping"),
        ("{}\n---\n{}\n", "one YAML document"),
        ("hosts:\n  lab: {}\n  lab: {}\n", "duplicate key"),
        ("1: value\n", "mapping keys must be strings"),
    ),
)
def test_strict_yaml_rejections(tmp_path: Path, text: str, diagnostic: str) -> None:
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, text))


def test_normalized_mapping_collisions_are_rejected(tmp_path: Path) -> None:
    document = inventory_document()
    document["hosts"]["host"]["path_mappings"] = {
        "/same/path": "/one",
        "/same/./path": "/two",
    }
    with pytest.raises(InventoryError, match="conflicting mapping"):
        load_inventory(write_inventory(tmp_path, document))


def test_target_or_recipe_must_supply_board(tmp_path: Path) -> None:
    document = change(inventory_document(), ("targets", "target", "board"), DELETE)
    with pytest.raises(InventoryError, match="board.*required"):
        load_inventory(write_inventory(tmp_path, document))


def test_render_rejects_invalid_default(tmp_path: Path) -> None:
    inventory = load_inventory(write_inventory(tmp_path, inventory_document()))
    with pytest.raises(ValueError, match="default"):
        render_product_config(inventory.host("host"), default_runner="invalid")
