# SPDX-License-Identifier: Apache-2.0

"""Shared pytest options for external validation layers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.hardware_support import prepared_hardware as _prepared_hardware
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


def _inventory_profile_ids(config: pytest.Config, capability: str) -> list[str]:
    """Return stable profile identifiers for collection-time parametrization."""
    path = hardware_config_path(config)
    if path is None:
        return ["__no_inventory__"]
    try:
        inventory = load_inventory(path)
    except (OSError, InventoryError):
        # The session fixture emits the detailed diagnostic at test setup.  A
        # placeholder keeps collection deterministic and allows pytest to
        # report the ordinary skip/failure instead of aborting collection.
        return ["__invalid_inventory__"]
    identifiers = [
        f"{target.id}:{profile.name}"
        for target in inventory.targets
        for profile in target.profiles
        if capability in profile.capabilities
    ]
    return identifiers or [f"__no_{capability}__"]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Expose each inventory capability profile as an independent test node."""
    capability_fixtures = {
        "flash_fixture": "flash",
        "debug_fixture": "debug",
        "attach_fixture": "attach",
        "debugserver_fixture": "debugserver",
        "thread_info_fixture": "thread_info",
        "rtt_fixture": "rtt",
        "semihosting_fixture": "semihosting",
    }
    for fixture_name, capability in capability_fixtures.items():
        if fixture_name in metafunc.fixturenames:
            params = _inventory_profile_ids(metafunc.config, capability)
            metafunc.parametrize(
                fixture_name,
                params,
                indirect=True,
                ids=params,
            )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Make strict external runs fail instead of accepting infrastructure skips."""
    if not os.environ.get("ZRO_STRICT_EXTERNAL"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len(reporter.stats.get("skipped", [])) if reporter is not None else 0
    if skipped:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _profile_record(request: pytest.FixtureRequest, records: list[dict]) -> dict:
    if request.param.startswith("__"):
        pytest.skip("hardware inventory has no matching capability profile")
    for record in records:
        if record["id"] == request.param:
            return record
    pytest.fail(f"inventory profile {request.param!r} was not prepared")


@pytest.fixture
def flash_fixture(request: pytest.FixtureRequest) -> dict:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"))


@pytest.fixture
def debug_fixture(request: pytest.FixtureRequest) -> dict:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"))


@pytest.fixture
def attach_fixture(request: pytest.FixtureRequest) -> dict:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"))


@pytest.fixture
def debugserver_fixture(request: pytest.FixtureRequest) -> dict:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"))


@pytest.fixture
def thread_info_fixture(
    request: pytest.FixtureRequest,
) -> dict:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"))


@pytest.fixture
def rtt_fixture(request: pytest.FixtureRequest) -> dict:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"))


@pytest.fixture
def semihosting_fixture(
    request: pytest.FixtureRequest,
) -> dict:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"))


@pytest.fixture
def ssh_host(hardware_inventory: Inventory) -> str:
    """Use the first declared host for transport-only SSH coverage."""
    return hardware_inventory.hosts[0].address


@pytest.fixture
def ssh_settings(hardware_inventory: Inventory):
    """Return the first host's transport settings for SSH integration tests."""
    return hardware_inventory.hosts[0]


# Re-export the session fixture so external modules can request it by name.
prepared_hardware = _prepared_hardware
