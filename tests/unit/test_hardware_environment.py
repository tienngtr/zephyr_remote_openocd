# SPDX-License-Identifier: Apache-2.0

"""Hardware operations must execute in the inventory-selected environment."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

import pytest

from tests.acceptance.debug import DebugAcceptance
from tests.acceptance.firmware import west_flash
from tests.acceptance.rtt import RttAcceptance
from tests.acceptance.semihosting import SemihostingAcceptance
from tests.elf_fixtures import ELF_WITNESS_OFFSET, elf_memory_witness_bytes
from tests.hardware_support import (
    DebugFixture,
    FirmwarePrecondition,
    PreparedTarget,
    RttFixture,
    SemihostingFixture,
)
from tests.inventory import (
    BuildEnvironment,
    DebugOperation,
    InventoryHost,
    RttOperation,
    SemihostingOperation,
    Toolchain,
)
from tests.support import ROOT


@pytest.mark.parametrize("operation", ("flash", "debug", "rtt", "rtt-flash", "semihosting"))
def test_hardware_launch_uses_selected_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    selected_base = tmp_path / "selected-zephyr"
    config = tmp_path / "generated-config.yaml"
    target = PreparedTarget(
        "target:profile",
        "target",
        "profile",
        InventoryHost("host", "unused", ("openocd",), ("ssh",), ("CHANNEL",), ()),
        BuildEnvironment(
            "build",
            selected_base,
            Path("west"),
            (
                ("ZEPHYR_TOOLCHAIN_VARIANT", "zephyr"),
                ("ZEPHYR_SDK_INSTALL_DIR", "/selected/sdk"),
                ("CHANNEL", "build"),
            ),
        ),
        Toolchain("toolchain", Path("gdb")),
        tmp_path / "build",
        config,
        (),
        (("CHANNEL", "profile"),),
    )
    for name, value in {
        "ZEPHYR_BASE": "/ambient/zephyr",
        "ZEPHYR_TOOLCHAIN_VARIANT": "ambient",
        "ZEPHYR_SDK_INSTALL_DIR": "/ambient/sdk",
        "CMAKE_PREFIX_PATH": "/ambient/cmake",
        "ZEPHYR_REMOTE_OPENOCD_CONFIG": "/ambient/config.yaml",
        "ZEPHYR_REMOTE_OPENOCD_REMOTE": "ambient-remote",
        "ZRO_RECORD": "1",
        "ZRO_RECORD_OPENOCD_VERSION": "ambient-version",
        "EXTRA_ZEPHYR_MODULES": "/ambient/module",
        "CHANNEL": "ambient",
        "PATH": "/host/command-path",
    }.items():
        monkeypatch.setenv(name, value)
    authentication = {
        "SSH_AUTH_SOCK": "/host/agent.sock",
        "SSH_ASKPASS": "/host/askpass",
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": ":test",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/host/session-bus",
        "XDG_RUNTIME_DIR": "/host/runtime",
        "XDG_SESSION_TYPE": "x11",
        "ICEAUTHORITY": "/host/iceauthority",
    }
    for name, value in authentication.items():
        monkeypatch.setenv(name, value)
    captured: dict[str, str] = {}

    class LaunchReached(Exception):
        pass

    def launch(*args: object, env: Mapping[str, str], **kwargs: object) -> NoReturn:
        captured.update(env)
        raise LaunchReached

    monkeypatch.setattr(subprocess, "run", launch)
    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(LaunchReached):
        if operation == "flash":
            west_flash(target, target.build_dir)
        elif operation == "debug":
            # Debug first establishes its precondition, which needs a witness.
            precondition = FirmwarePrecondition(tmp_path / "before")
            selected = bytearray(elf_memory_witness_bytes())
            selected[ELF_WITNESS_OFFSET] ^= 0xFF
            for elf, image in (
                (precondition.elf_file, elf_memory_witness_bytes()),
                (target.elf_file, selected),
            ):
                elf.parent.mkdir(parents=True)
                elf.write_bytes(image)
            DebugAcceptance().debug(
                DebugFixture(target, DebugOperation("before", "main", ()), precondition)
            )
        elif operation in ("rtt", "rtt-flash"):
            rtt = RttFixture(target, RttOperation(12345, "pong", "ping", 30, True))
            if operation == "rtt":
                with RttAcceptance().start(rtt, "debugserver"):
                    pass
            else:
                RttAcceptance().program(rtt)
        else:
            SemihostingAcceptance().run(
                SemihostingFixture(target, SemihostingOperation((), (), "ready", 30))
            )

    assert captured["ZEPHYR_BASE"] == str(selected_base)
    assert captured["ZEPHYR_TOOLCHAIN_VARIANT"] == "zephyr"
    assert captured["ZEPHYR_SDK_INSTALL_DIR"] == "/selected/sdk"
    assert captured["CHANNEL"] == "profile"
    assert captured["ZEPHYR_REMOTE_OPENOCD_CONFIG"] == str(config)
    assert captured["EXTRA_ZEPHYR_MODULES"] == str(ROOT)
    assert captured["PATH"] == "/host/command-path"
    assert {name: captured[name] for name in authentication} == authentication
    assert (
        not {
            "CMAKE_PREFIX_PATH",
            "ZEPHYR_REMOTE_OPENOCD_REMOTE",
            "ZRO_RECORD",
            "ZRO_RECORD_OPENOCD_VERSION",
        }
        & captured.keys()
    )
