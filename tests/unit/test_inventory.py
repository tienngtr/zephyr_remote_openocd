# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from zephyr_remote_openocd.config import load_config, resolve_remote

from tests.inventory import (
    AttachOperation,
    DebugOperation,
    FlashOperation,
    InventoryError,
    RttOperation,
    load_inventory,
    render_product_config,
)
from tests.support import ROOT

STARTER_EXAMPLE = ROOT / "tests/fixtures/hardware.example.yaml"
EXAMPLE = ROOT / "tests/fixtures/hardware.complete.example.yaml"
DELETE = object()


def example_document() -> dict:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_starter_example_is_minimal_and_valid() -> None:
    inventory = load_inventory(STARTER_EXAMPLE)
    target = inventory.target("stm32f746g_disco")
    assert [profile.name for profile in target.profiles] == ["flash"]
    assert target.profile("flash").operation_names == ("flash",)


def test_schema_documents_every_named_property_and_array_item() -> None:
    schema_path = ROOT / "tests/fixtures/hardware.schema.json"
    schema = json.loads(schema_path.read_text())

    def check(node: object, location: str = "schema") -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                check(item, f"{location}[{index}]")
            return
        if not isinstance(node, dict):
            return
        for name, value in node.get("properties", {}).items():
            assert "description" in value, f"{location}.properties.{name} lacks description"
        for keyword in ("items", "prefixItems"):
            value = node.get(keyword)
            if isinstance(value, dict):
                assert "description" in value, f"{location}.{keyword} lacks description"
        for key, value in node.items():
            check(value, f"{location}.{key}")

    check(schema)


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


def test_example_is_complete_and_renderable(tmp_path: Path) -> None:
    inventory = load_inventory(EXAMPLE)
    assert inventory.build_environment("zephyr44").west == Path(
        "/path/to/zephyrproject/.venv/bin/west"
    )
    assert inventory.toolchain("arm").gdb == Path(
        "/path/to/zephyr-sdk/arm-zephyr-eabi/bin/arm-zephyr-eabi-gdb"
    )
    host = inventory.host("lab")
    assert host.forward_env == ("PROBE_CHANNEL",)
    assert host.openocd_command == ("/absolute/remote/path/to/openocd",)
    target = inventory.target("stm32f746g_disco")
    assert target.build("hello").application == "samples/hello_world"
    assert target.endpoint("console").baud == 115200
    flash = target.profile("core")
    assert flash.environment == (("PROBE_CHANNEL", "0"),)
    assert isinstance(flash.operation("flash"), FlashOperation)
    debug = target.profile("core")
    assert debug.operation_names == ("flash", "debug", "attach", "debugserver")
    assert isinstance(debug.operation("debug"), DebugOperation)
    assert isinstance(debug.operation("attach"), AttachOperation)
    assert isinstance(target.profile("rtt").operation("rtt"), RttOperation)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(render_product_config(host), encoding="utf-8")
    assert load_config(config_path).default_runner == "openocd"


@pytest.mark.parametrize("name", ("lab", "on", "off", "true", "null"))
def test_rendered_inventory_round_trips_through_product_schema(tmp_path, name):
    document = example_document()
    document["hosts"][name] = document["hosts"].pop("lab")
    document["targets"]["stm32f746g_disco"]["host"] = name
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
        (("hosts", "lab", "ssh_host"), DELETE, "ssh_host"),
        (("hosts", "lab", "openocd_command"), ["openocd"], "openocd_command"),
        (
            ("targets", "stm32f746g_disco", "profiles", "core", "capabilities"),
            ["debug"],
            "capabilities",
        ),
        (
            (
                "targets",
                "stm32f746g_disco",
                "profiles",
                "core",
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
                "stm32f746g_disco",
                "profiles",
                "core",
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
                "stm32f746g_disco",
                "profiles",
                "rtt",
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
    document = change(example_document(), path, value)
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, document))


@pytest.mark.parametrize(
    ("path", "value", "diagnostic"),
    (
        (("targets", "stm32f746g_disco", "host"), "missing", "unknown host"),
        (
            ("targets", "stm32f746g_disco", "build_environment"),
            "missing",
            "unknown build environment",
        ),
        (("targets", "stm32f746g_disco", "toolchain"), "missing", "unknown toolchain"),
        (
            ("targets", "stm32f746g_disco", "profiles", "core", "build"),
            "missing",
            "unknown build",
        ),
        (
            ("targets", "stm32f746g_disco", "profiles", "core", "environment"),
            {"OTHER": "1"},
            "allow-list",
        ),
        (
            (
                "targets",
                "stm32f746g_disco",
                "profiles",
                "core",
                "operations",
                "flash",
                "precondition_build",
            ),
            "hello",
            "must differ",
        ),
        (
            (
                "targets",
                "stm32f746g_disco",
                "profiles",
                "core",
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
                "stm32f746g_disco",
                "profiles",
                "core",
                "operations",
                "flash",
                "serial",
                "endpoint",
            ),
            "missing",
            "unknown serial",
        ),
        (
            ("targets", "stm32f746g_disco", "builds", "hello", "application"),
            "../escape",
            "escape",
        ),
    ),
)
def test_semantic_references_are_validated(tmp_path, path, value, diagnostic) -> None:
    document = change(example_document(), path, value)
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, document))


def test_direct_gdb_operations_require_a_toolchain(tmp_path: Path) -> None:
    document = change(example_document(), ("targets", "stm32f746g_disco", "toolchain"), DELETE)
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
    document = example_document()
    document["hosts"]["lab"]["path_mappings"] = {
        "/same/path": "/one",
        "/same/./path": "/two",
    }
    with pytest.raises(InventoryError, match="conflicting mapping"):
        load_inventory(write_inventory(tmp_path, document))


def test_target_or_recipe_must_supply_board(tmp_path: Path) -> None:
    document = change(example_document(), ("targets", "stm32f746g_disco", "board"), DELETE)
    with pytest.raises(InventoryError, match="board.*required"):
        load_inventory(write_inventory(tmp_path, document))


def test_render_rejects_invalid_default() -> None:
    inventory = load_inventory(EXAMPLE)
    with pytest.raises(ValueError, match="default"):
        render_product_config(inventory.host("lab"), default_runner="invalid")
