# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
from zephyr_remote_openocd.config import load_config, resolve_remote

from tests.inventory import InventoryError, load_inventory, render_product_config
from tests.support import ROOT

EXAMPLE = ROOT / "tests" / "fixtures" / "hardware.example.toml"


def write_inventory(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "hardware.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_is_complete_and_renderable(tmp_path: Path) -> None:
    inventory = load_inventory(EXAMPLE)
    assert inventory.hosts[0].forward_env == ("FTDI_CHANNEL",)
    target = inventory.target("board")
    assert target.build("hello").application == "samples/hello_world"
    assert target.build("minimal").application == "samples/basic/minimal"
    assert target.endpoint("console").baud == 115200
    assert target.profile("flash").environment == (("FTDI_CHANNEL", "0"),)
    debug = target.profile("debug")
    assert debug.debug is not None
    assert debug.debug.breakpoint == "main"
    assert debug.attach is not None
    assert debug.attach.precondition_build == "minimal"
    flash = target.profile("flash")
    assert flash.flash is not None
    assert flash.flash.precondition_build == "minimal"
    rendered = render_product_config(inventory.host("lab"), default_runner="remote_openocd")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(rendered, encoding="utf-8")
    assert load_config(config_path).default_runner == "remote_openocd"


@pytest.mark.parametrize(
    ("fragment", "diagnostic"),
    (
        ("future = true\n", "unknown key"),
        ("[[hosts]]\nid = \"lab\"\n", r"hosts\[0\]\.address"),
        (
            "[[hosts]]\nid=\"lab\"\naddress=\"x\"\nopenocd=\"openocd\"\n",
            r"hosts\[0\]\.openocd",
        ),
    ),
)
def test_inventory_rejects_bad_top_level_and_host(
    tmp_path: Path, fragment: str, diagnostic: str
) -> None:
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, fragment))


@pytest.mark.parametrize("name", ("lab", "on", "off", "true", "null"))
def test_rendered_inventory_round_trips_through_product_schema(tmp_path, name):
    text = EXAMPLE.read_text().replace('"lab"', f'"{name}"')
    inventory = load_inventory(write_inventory(tmp_path, text))
    host = inventory.host(name)
    path = tmp_path / "config.yaml"
    path.write_text(render_product_config(host))
    selected = resolve_remote(load_config(path))
    assert selected.name == name
    assert selected.ssh_host == host.address
    assert selected.openocd_command == (host.openocd,)
    assert selected.ssh_command == host.ssh_command
    assert selected.forward_env == host.forward_env
    assert [(item.local, item.remote) for item in selected.path_mappings] == [
        (item.local, item.remote) for item in host.path_mappings
    ]


def valid_prefix() -> str:
    return (
        '[[hosts]]\nid = "lab"\naddress = "host"\n'
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
            "[targets.profiles.default]\ncapabilities = [\"debug\"]\nbuild = \"hello\"\n",
            "debug capability",
        ),
        (
            "[targets.profiles.default]\ncapabilities = [\"attach\"]\nbuild = \"hello\"\n",
            "attach capability",
        ),
        (
            "[targets.profiles.default]\ncapabilities = [\"debug\"]\nbuild = \"hello\"\n"
            "[targets.profiles.default.debug]\nbreakpoint = \"main + 4\"\n",
            "breakpoint",
        ),
        (
            "[targets.profiles.default]\ncapabilities = [\"debug\"]\nbuild = \"hello\"\n"
            "[targets.profiles.default.debug]\nbreakpoint = \"main\"\nfuture = true\n",
            "unknown key",
        ),
        (
            "[targets.profiles.default]\ncapabilities = [\"flash\"]\nbuild = \"missing\"\n",
            "unknown build",
        ),
        (
            "[targets.profiles.default]\ncapabilities = [\"flash\"]\nbuild = \"hello\"\n",
            "flash capability",
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
        '[targets.profiles.default.flash]\nprecondition_build = "minimal"\n'
        'quiescence_timeout = 2\n'
        '[targets.profiles.default.debug]\nbreakpoint = "main"\n'
        '[targets.profiles.default.attach]\nprecondition_build = "minimal"\n'
        '[targets.profiles.rtt]\ncapabilities = ["rtt"]\nbuild = "hello"\n'
        'environment = {}\n[targets.profiles.rtt.rtt]\nport = 20000\n'
        'response = "ok"\ntimeout = 1\nprogram_survives_reset = true\n'
        '[targets.profiles.rtt.debug]\nbreakpoint = "main"\n'
    )
    prefix = valid_prefix() + '[targets.builds.minimal]\napplication = "samples/basic/minimal"\n'
    inventory = load_inventory(write_inventory(tmp_path, prefix + new))
    target = inventory.target("board")
    assert target.endpoint("console").data_bits == 8
    assert {profile.name for profile in target.profiles} == {"default", "rtt"}
    default = target.profile("default")
    assert default.capabilities == ("flash", "debug", "attach", "debugserver")
    assert default.debug is not None
    assert default.debug.breakpoint == "main"


@pytest.mark.parametrize(
    ("flash_table", "diagnostic"),
    (
        ('precondition_build = "hello"\nquiescence_timeout = 2\n', "must differ"),
        ('precondition_build = "missing"\nquiescence_timeout = 2\n', "unknown build"),
        ('precondition_build = "minimal"\n', "quiescence_timeout"),
        (
            'precondition_build = "minimal"\nquiescence_timeout = 0\n',
            "positive number",
        ),
    ),
)
def test_flash_precondition_contract(tmp_path: Path, flash_table: str, diagnostic: str) -> None:
    text = (
        valid_prefix()
        + '[targets.builds.minimal]\napplication = "samples/basic/minimal"\n'
        + '[targets.profiles.flash]\ncapabilities = ["flash"]\nbuild = "hello"\n'
        + '[targets.profiles.flash.flash]\n'
        + flash_table
    )
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, text))


@pytest.mark.parametrize(
    ("precondition", "diagnostic"),
    (("hello", "must differ"), ("missing", "unknown build")),
)
def test_attach_precondition_contract(tmp_path: Path, precondition: str, diagnostic: str) -> None:
    text = (
        valid_prefix()
        + '[targets.builds.minimal]\napplication = "samples/basic/minimal"\n'
        + '[targets.profiles.attach]\ncapabilities = ["attach"]\nbuild = "hello"\n'
        + '[targets.profiles.attach.attach]\n'
        + f'precondition_build = "{precondition}"\n'
    )
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, text))


@pytest.mark.parametrize(
    ("survival", "diagnostic"),
    (("", "program_survives_reset"), ("false", "must be true")),
)
def test_rtt_requires_reset_persistence(tmp_path: Path, survival: str, diagnostic: str) -> None:
    setting = f"program_survives_reset = {survival}\n" if survival else ""
    text = (
        valid_prefix()
        + '[targets.profiles.rtt]\ncapabilities = ["rtt"]\nbuild = "hello"\n'
        + '[targets.profiles.rtt.rtt]\nport = 19021\nresponse = "ok"\ntimeout = 2\n'
        + setting
        + '[targets.profiles.rtt.debug]\nbreakpoint = "main"\n'
    )
    with pytest.raises(InventoryError, match=diagnostic):
        load_inventory(write_inventory(tmp_path, text))


def test_duplicate_host_and_mapping_are_rejected(tmp_path: Path) -> None:
    duplicate_hosts = (
        '[[hosts]]\nid = "lab"\naddress = "one"\n'
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
        render_product_config(inventory.host("lab"), default_runner="invalid")
