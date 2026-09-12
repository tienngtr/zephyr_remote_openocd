# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from tests import support
from tests.conftest import hardware_config_path
from tests.support import find_repository_root


class Config:
    def __init__(self, option):
        self.option = option

    def getoption(self, _name):
        return self.option


def test_hardware_config_cli_option_has_precedence(monkeypatch):
    monkeypatch.setenv("ZRO_HARDWARE_CONFIG", "/from/environment.toml")
    assert hardware_config_path(Config("/from/cli.toml")) == Path("/from/cli.toml")


def test_hardware_config_environment_fallback(monkeypatch):
    monkeypatch.setenv("ZRO_HARDWARE_CONFIG", "/from/environment.toml")
    assert hardware_config_path(Config(None)) == Path("/from/environment.toml")


def test_hardware_config_is_optional_for_default_collection(monkeypatch):
    monkeypatch.delenv("ZRO_HARDWARE_CONFIG", raising=False)
    assert hardware_config_path(Config(None)) is None


def test_repository_root_is_found_by_markers_not_directory_depth(tmp_path):
    root = tmp_path / "module"
    nested = root / "changed" / "directory" / "layout"
    (root / "zephyr").mkdir(parents=True)
    (root / "zephyr" / "module.yml").write_text("name: example\n")
    (root / "python" / "zephyr_remote_openocd").mkdir(parents=True)
    nested.mkdir(parents=True)

    assert find_repository_root(nested) == root


def test_repository_root_requires_complete_markers(tmp_path):
    (tmp_path / "zephyr").mkdir()
    (tmp_path / "zephyr" / "module.yml").write_text("name: example\n")

    with pytest.raises(RuntimeError, match="repository root"):
        find_repository_root(tmp_path)


@pytest.mark.parametrize(
    ("release", "version", "expected_wsl", "expected_wsl2"),
    (
        ("5.15.90.1-microsoft-standard-WSL2", "Microsoft WSL2", True, True),
        ("4.4.0-19041-Microsoft", "Microsoft", True, False),
        ("6.8.0-generic", "Ubuntu", False, False),
    ),
)
def test_wsl_detection_distinguishes_native_wsl1_and_wsl2(
    monkeypatch, release, version, expected_wsl, expected_wsl2
):
    monkeypatch.setattr(support.sys, "platform", "linux")
    contents = {"osrelease": release, "version": version}
    monkeypatch.setattr(support.Path, "read_text", lambda path: contents[path.name])

    assert support.is_wsl() is expected_wsl
    assert support.is_wsl2() is expected_wsl2
