# SPDX-License-Identifier: Apache-2.0

"""Shared pytest options for external validation layers."""

from __future__ import annotations

import errno
import socket
from pathlib import Path

import pytest

from tests.hardware_support import (
    BuildTiming,
    HardwarePreparation,
    PreparedOperation,
    hardware_cache_root,
    hardware_shared_cache_root,
)
from tests.inventory import Inventory, InventoryError, load_inventory

_HARDWARE_BUILD_TIMINGS = pytest.StashKey[list[BuildTiming]]()
_HARDWARE_TEST_TIMINGS = pytest.StashKey[list[tuple[str, str, float]]]()


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_HARDWARE_TEST_TIMINGS] = []


@pytest.fixture(scope="session")
def requires_loopback_listener() -> None:
    """Skip tests that need a loopback TCP listener when it is unavailable."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EPERM}:
            pytest.skip("loopback TCP listeners unavailable in this test environment")
        raise


@pytest.fixture(autouse=True)
def isolated_product_environment(monkeypatch):
    """Tests opt into product settings, never inherit the developer's selection."""
    for name in (
        "ZEPHYR_REMOTE_OPENOCD_CONFIG",
        "ZEPHYR_REMOTE_OPENOCD_REMOTE",
        "ZRO_RECORD",
        "ZRO_RECORD_OPENOCD_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("external validation")
    group.addoption(
        "--hardware-inventory",
        action="store",
        default=None,
        help="path to the local YAML hardware inventory",
    )
    group.addoption(
        "--require-external-tests",
        action="store_true",
        default=False,
        help="fail the run if any test is skipped during external validation",
    )
    group.addoption(
        "--hardware-timings",
        action="store_true",
        default=False,
        help="report hardware build and pytest phase timings",
    )


def hardware_inventory_path(config: pytest.Config) -> Path | None:
    """Resolve CLI inventory selection without touching the filesystem."""
    value = config.getoption("--hardware-inventory")
    return Path(value).expanduser().resolve() if value else None


@pytest.fixture(scope="session")
def hardware_inventory(pytestconfig: pytest.Config) -> Inventory:
    """Load the configured inventory, skipping normal runs with no fixture."""
    path = hardware_inventory_path(pytestconfig)
    if path is None:
        pytest.skip("hardware inventory is not configured; pass --hardware-inventory")
    try:
        return load_inventory(path)
    except InventoryError as error:
        pytest.fail(str(error))


def _inventory_profile_ids(config: pytest.Config, capability: str) -> list[str]:
    """Return stable profile identifiers for collection-time parametrization."""
    path = hardware_inventory_path(config)
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
        f"{target.name}:{profile.name}"
        for target in inventory.targets
        for profile in target.profiles
        if capability in profile.operation_names
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
    """Make required external-test runs fail instead of accepting skips."""
    if not session.config.getoption("--require-external-tests"):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = len(reporter.stats.get("skipped", [])) if reporter is not None else 0
    if skipped and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    del call
    outcome = yield
    report = outcome.get_result()
    if item.get_closest_marker("hardware") is not None:
        item.config.stash[_HARDWARE_TEST_TIMINGS].append(
            (report.nodeid, report.when, report.duration)
        )


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print opt-in timings for external hardware validation."""
    del exitstatus
    if not config.getoption("--hardware-timings"):
        return
    build_timings = config.stash.get(_HARDWARE_BUILD_TIMINGS, [])
    test_timings = config.stash.get(_HARDWARE_TEST_TIMINGS, [])
    if not build_timings and not test_timings:
        return
    terminalreporter.write_sep("=", "hardware timings")
    for timing in build_timings:
        terminalreporter.write_line(
            f"build {timing.target}:{timing.build} {timing.cache_state} {timing.duration:.2f}s"
        )
    for nodeid, phase, duration in test_timings:
        terminalreporter.write_line(f"test {phase} {duration:.2f}s {nodeid}")


def _profile_record(
    request: pytest.FixtureRequest,
    preparation: HardwarePreparation,
    operation: str,
) -> PreparedOperation:
    if request.param.startswith("__"):
        pytest.skip("hardware inventory has no matching capability profile")
    return preparation.prepare(request.param, operation)


@pytest.fixture
def flash_fixture(request: pytest.FixtureRequest) -> PreparedOperation:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"), "flash")


@pytest.fixture
def debug_fixture(request: pytest.FixtureRequest) -> PreparedOperation:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"), "debug")


@pytest.fixture
def attach_fixture(request: pytest.FixtureRequest) -> PreparedOperation:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"), "attach")


@pytest.fixture
def debugserver_fixture(request: pytest.FixtureRequest) -> PreparedOperation:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"), "debugserver")


@pytest.fixture
def thread_info_fixture(
    request: pytest.FixtureRequest,
) -> PreparedOperation:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"), "thread_info")


@pytest.fixture
def rtt_fixture(request: pytest.FixtureRequest) -> PreparedOperation:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"), "rtt")


@pytest.fixture
def semihosting_fixture(
    request: pytest.FixtureRequest,
) -> PreparedOperation:
    return _profile_record(request, request.getfixturevalue("prepared_hardware"), "semihosting")


@pytest.fixture
def ssh_host(hardware_inventory: Inventory) -> str:
    """Use the first declared host for transport-only SSH coverage."""
    return hardware_inventory.hosts[0].ssh_host


@pytest.fixture
def ssh_settings(hardware_inventory: Inventory):
    """Return the first host's transport settings for SSH integration tests."""
    return hardware_inventory.hosts[0]


@pytest.fixture(scope="session")
def prepared_hardware(
    hardware_inventory: Inventory, pytestconfig: pytest.Config
) -> HardwarePreparation:
    cache_root = hardware_cache_root(hardware_inventory)
    preparation = HardwarePreparation(
        hardware_inventory,
        cache_root / "builds",
        cache_root / "configs",
        cache_root=hardware_shared_cache_root,
    )
    pytestconfig.stash[_HARDWARE_BUILD_TIMINGS] = preparation.build_timings
    return preparation
