# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

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
