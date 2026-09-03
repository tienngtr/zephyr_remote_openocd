# SPDX-License-Identifier: Apache-2.0

"""Shared pytest options for external validation layers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.inventory import Inventory, InventoryError, load_inventory


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("external validation")
    group.addoption(
        "--hardware-config",
        action="store",
        default=None,
        help="path to the ignored TOML hardware inventory (or use ZRO_HARDWARE_CONFIG)",
    )


def hardware_config_path(config: pytest.Config) -> Path | None:
    """Resolve CLI-first inventory selection without touching the filesystem."""
    value = config.getoption("--hardware-config") or os.environ.get("ZRO_HARDWARE_CONFIG")
    return Path(value).expanduser().resolve() if value else None


@pytest.fixture(scope="session")
def hardware_inventory(pytestconfig: pytest.Config) -> Inventory:
    """Load the configured inventory, skipping normal runs with no fixture."""
    path = hardware_config_path(pytestconfig)
    if path is None:
        pytest.skip("hardware inventory is not configured; pass --hardware-config")
    try:
        return load_inventory(path)
    except InventoryError as error:
        pytest.fail(str(error))
