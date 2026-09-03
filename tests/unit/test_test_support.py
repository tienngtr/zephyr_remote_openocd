# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from tests.conftest import hardware_config_path


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
