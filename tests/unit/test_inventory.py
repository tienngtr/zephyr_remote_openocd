# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from tests.inventory import InventoryError, load_inventory, render_product_config
from tests.support import ROOT

EXAMPLE = ROOT / "tests" / "fixtures" / "hardware.example.toml"


def write_inventory(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "hardware.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_neutral_example_is_complete_and_renderable() -> None:
    inventory = load_inventory(EXAMPLE)
    assert inventory.schema_version == 1
    assert inventory.hosts[0].forward_env == ("FTDI_CHANNEL",)
    target = inventory.target("board")
    assert target.build("hello").application == "samples/hello_world"
    assert target.endpoint("console").baud == 115200
    assert target.profiles[0].environment == (("FTDI_CHANNEL", "0"),)
    rendered = render_product_config(inventory.host("lab"), default="remote")
    assert '[zephyr]\ndefault = "remote"' in rendered
    assert 'openocd = "/absolute/path/to/openocd"' in rendered


@pytest.mark.parametrize(
    ("fragment", "diagnostic"),
    (
        ("schema_version = 2\n", "schema_version"),
        ("future = true\n", "unknown key"),
        ("schema_version = 1\n[[hosts]]\nid = \"lab\"\n", r"hosts\[0\]\.address"),
        (
            "schema_version = 1\n[[hosts]]\nid=\"lab\"\naddress=\"x\"\nopenocd=\"openocd\"\n",
            r"hosts\[0\]\.openocd",
        ),
    ),
)
def test_inventory_rejects_bad_top_level_and_host(
    tmp_path: Path, fragment: str, diagnostic: str
) -> None:
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, fragment))


def valid_prefix() -> str:
    return (
        'schema_version = 1\n[[hosts]]\nid = "lab"\naddress = "host"\n'
        'openocd = "/opt/openocd"\nforward_env = ["CHANNEL"]\n'
        '[[targets]]\nid = "board"\nhost = "lab"\nzephyr_base = "/zephyr"\n'
        'west = "/west"\nboard = "vendor/board"\n[targets.builds.hello]\n'
        'application = "samples/hello_world"\n'
        ''
    )


@pytest.mark.parametrize(
    ("suffix", "diagnostic"),
    (
        ("[targets.profiles.default]\nfuture = true\n", "unknown key"),
        (
            "[targets.profiles.default]\ncapabilities = [\"rtt\"]\nbuild = \"hello\"\n",
            "rtt capability",
        ),
        (
            "[targets.profiles.default]\ncapabilities = [\"flash\"]\nbuild = \"missing\"\n",
            "unknown build",
        ),
        (
            "[targets.profiles.default]\ncapabilities = [\"flash\"]\n"
            "build = \"hello\"\nenvironment = { OTHER = \"1\" }\n",
            "allow-list",
        ),
    ),
)
def test_inventory_rejects_bad_references_and_profile_data(
    tmp_path: Path, suffix: str, diagnostic: str
) -> None:
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(
            write_inventory(
                tmp_path,
                valid_prefix()
                + suffix.replace("[targets.profiles.default]\n", "[targets.profiles.default]\n"),
            )
        )


def test_serial_framing_and_capabilities_are_independent(tmp_path: Path) -> None:
    new = (
        '[targets.serial.console]\ndevice = "/dev/tty"\nbaud = 921600\n'
        'pattern = "ready"\ntimeout = 2\n[targets.profiles.default]\n'
        'capabilities = ["flash", "debug", "attach", "debugserver"]\n'
        'build = "hello"\nserial = "console"\nenvironment = {}\n'
        '[targets.profiles.rtt]\ncapabilities = ["rtt"]\nbuild = "hello"\n'
        'environment = {}\n[targets.profiles.rtt.rtt]\nport = 20000\n'
        'response = "ok"\ntimeout = 1\n'
    )
    inventory = load_inventory(write_inventory(tmp_path, valid_prefix() + new))
    target = inventory.target("board")
    assert target.endpoint("console").data_bits == 8
    assert {profile.name for profile in target.profiles} == {"default", "rtt"}
    assert target.profiles[0].capabilities == ("flash", "debug", "attach", "debugserver")


def test_duplicate_host_and_mapping_are_rejected(tmp_path: Path) -> None:
    duplicate_hosts = (
        'schema_version = 1\n[[hosts]]\nid = "lab"\naddress = "one"\n'
        'openocd = "/opt/openocd"\n[[hosts]]\nid = "lab"\naddress = "two"\n'
        'openocd = "/opt/openocd"\n'
    )
    with pytest.raises(InventoryError, match="hosts.*names must be unique"):
        load_inventory(write_inventory(tmp_path, duplicate_hosts))

    text = valid_prefix().replace(
        'forward_env = ["CHANNEL"]',
        'forward_env = ["CHANNEL"]\n[[hosts.path_mappings]]\nlocal = "/same"\n'
        'remote = "/one"\n[[hosts.path_mappings]]\nlocal = "/same"\nremote = "/two"',
    )
    with pytest.raises(InventoryError, match="conflicting mapping"):
        load_inventory(write_inventory(tmp_path, text))


def test_render_rejects_invalid_default() -> None:
    inventory = load_inventory(EXAMPLE)
    with pytest.raises(ValueError, match="default"):
        render_product_config(inventory.host("lab"), default="invalid")
