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

    def getoption(self, name: str) -> str | None:
        assert name == "--hardware-config"
        return self.option


def config(option: str | None) -> pytest.Config:
    return cast(pytest.Config, Config(option))


def test_hardware_config_environment_fallback(monkeypatch) -> None:
    monkeypatch.setenv("ZRO_HARDWARE_CONFIG", "/from/environment.yaml")
    assert hardware_config_path(config(None)) == Path("/from/environment.yaml")


def test_hardware_config_is_optional_for_default_collection(monkeypatch) -> None:
    monkeypatch.delenv("ZRO_HARDWARE_CONFIG", raising=False)
    assert hardware_config_path(config(None)) is None
