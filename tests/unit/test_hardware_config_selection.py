# SPDX-License-Identifier: Apache-2.0

"""Safety-relevant selection behavior for external hardware inventories."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from tests.conftest import hardware_config_path


class Config:
    def __init__(self, option: str | None):
        self.option = option

    def getoption(self, _name: str) -> str | None:
        return self.option


def config(option: str | None) -> pytest.Config:
    return cast(pytest.Config, Config(option))


def test_hardware_config_cli_option_has_precedence(monkeypatch) -> None:
    monkeypatch.setenv("ZRO_HARDWARE_CONFIG", "/from/environment.yaml")
    assert hardware_config_path(config("/from/cli.yaml")) == Path("/from/cli.yaml")


def test_hardware_config_environment_fallback(monkeypatch) -> None:
    monkeypatch.setenv("ZRO_HARDWARE_CONFIG", "/from/environment.yaml")
    assert hardware_config_path(config(None)) == Path("/from/environment.yaml")


def test_hardware_config_is_optional_for_default_collection(monkeypatch) -> None:
    monkeypatch.delenv("ZRO_HARDWARE_CONFIG", raising=False)
    assert hardware_config_path(config(None)) is None
