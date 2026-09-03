# SPDX-License-Identifier: Apache-2.0

"""Parser for the ignored hardware-validation inventory.

This module is test infrastructure, not part of the product configuration
surface.  The inventory deliberately has a small, explicit schema so fixture
files remain reviewable and cannot silently acquire new behavior.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class InventoryError(ValueError):
    """An actionable inventory validation error."""


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAPABILITIES = frozenset(
    {"flash", "debug", "attach", "debugserver", "thread_info", "rtt", "semihosting"}
)


@dataclass(frozen=True)
class InventoryPathMapping:
    local: Path
    remote: PurePosixPath


@dataclass(frozen=True)
class InventoryHost:
    id: str
    address: str
    ssh_command: tuple[str, ...]
    openocd: str
    forward_env: tuple[str, ...]
    path_mappings: tuple[InventoryPathMapping, ...]


@dataclass(frozen=True)
class BuildRecipe:
    name: str
    application: str
    board: str
    west_args: tuple[str, ...]
    cmake_args: tuple[str, ...]


@dataclass(frozen=True)
class SerialEndpoint:
    name: str
    device: str
    baud: int
    data_bits: int
    parity: str
    stop_bits: int
    flow_control: str
    pattern: str
    timeout: float


@dataclass(frozen=True)
class Expectations:
    patterns: tuple[str, ...]
    thread_info_pattern: str | None


@dataclass(frozen=True)
class RttExpectation:
    port: int
    response: str
    input: str
    timeout: float


@dataclass(frozen=True)
class SemihostingExpectation:
    commands: tuple[str, ...]
    gdb_commands: tuple[str, ...]
    output: str
    timeout: float


@dataclass(frozen=True)
class OperationProfile:
    name: str
    capabilities: tuple[str, ...]
    build: str
    serial: str | None
    probe_serial: str | None
    runner_args: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    expectations: Expectations
    rtt: RttExpectation | None
    semihosting: SemihostingExpectation | None


@dataclass(frozen=True)
class InventoryTarget:
    id: str
    host: str
    zephyr_base: Path
    west: Path
    gdb: Path | None
    board: str | None
    builds: tuple[BuildRecipe, ...]
    serial: tuple[SerialEndpoint, ...]
    profiles: tuple[OperationProfile, ...]

    def build(self, name: str) -> BuildRecipe:
        for recipe in self.builds:
            if recipe.name == name:
                return recipe
        raise KeyError(name)

    def endpoint(self, name: str) -> SerialEndpoint:
        for endpoint in self.serial:
            if endpoint.name == name:
                return endpoint
        raise KeyError(name)


@dataclass(frozen=True)
class Inventory:
    path: Path
    schema_version: int
    hosts: tuple[InventoryHost, ...]
    targets: tuple[InventoryTarget, ...]

    def host(self, name: str) -> InventoryHost:
        for host in self.hosts:
            if host.id == name:
                return host
        raise KeyError(name)

    def target(self, name: str) -> InventoryTarget:
        for target in self.targets:
            if target.id == name:
                return target
        raise KeyError(name)


def _error(path: Path, location: str, message: str) -> InventoryError:
    return InventoryError(f"invalid {location} in {path}: {message}")


def _table(value: Any, path: Path, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, location, "expected a TOML table")
    return value


def _keys(table: dict[str, Any], allowed: set[str], path: Path, location: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise _error(path, location, "unknown key(s): " + ", ".join(unknown))


def _required_string(table: dict[str, Any], key: str, path: Path, location: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise _error(path, f"{location}.{key}", "expected a non-empty string")
    return value


def _optional_string(table: dict[str, Any], key: str, path: Path, location: str) -> str | None:
    if key not in table or table[key] is None:
        return None
    return _required_string(table, key, path, location)


def _identifier(value: str, path: Path, location: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise _error(
            path, location, "must start with a letter and contain only letters, digits, _ or -"
        )
    return value


def _string_array(
    value: Any, path: Path, location: str, *, nonempty: bool = False
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or not all(isinstance(item, str) and item.strip() and "\0" not in item for item in value)
    ):
        expected = "a non-empty string array" if nonempty else "a string array"
        raise _error(path, location, f"expected {expected}")
    return tuple(value)


def _absolute_remote(value: str, path: Path, location: str) -> str:
    posix = PurePosixPath(value)
    if (
        not posix.is_absolute()
        or value.startswith("//")
        or (value != "/" and any(part in {"", ".", ".."} for part in value.split("/")[1:]))
    ):
        raise _error(path, location, "expected a normalized absolute POSIX path")
    return value


def _local_path(value: str, path: Path, location: str) -> Path:
    try:
        expanded = Path(value).expanduser()
    except (OSError, RuntimeError) as error:
        raise _error(path, location, str(error)) from error
    if not expanded.is_absolute():
        raise _error(path, location, "expected an absolute path or ~ path")
    return expanded.resolve()


def _unique_names(items: list[str], path: Path, location: str) -> None:
    if len(items) != len(set(items)):
        raise _error(path, location, "names must be unique")


def _path_mappings(value: Any, path: Path, location: str) -> tuple[InventoryPathMapping, ...]:
    if not isinstance(value, list):
        raise _error(path, location, "expected an array of tables")
    result: list[InventoryPathMapping] = []
    locals_seen: dict[Path, PurePosixPath] = {}
    for index, raw in enumerate(value):
        item_location = f"{location}[{index}]"
        item = _table(raw, path, item_location)
        _keys(item, {"local", "remote"}, path, item_location)
        local = _local_path(
            _required_string(item, "local", path, item_location), path, f"{item_location}.local"
        )
        remote = PurePosixPath(
            _absolute_remote(
                _required_string(item, "remote", path, item_location),
                path,
                f"{item_location}.remote",
            )
        )
        if local in locals_seen:
            kind = "duplicate" if locals_seen[local] == remote else "conflicting"
            raise _error(path, item_location, f"{kind} mapping for {local}")
        locals_seen[local] = remote
        result.append(InventoryPathMapping(local, remote))
    return tuple(result)


def _host(raw: Any, path: Path, index: int) -> InventoryHost:
    location = f"hosts[{index}]"
    item = _table(raw, path, location)
    _keys(
        item,
        {"id", "address", "ssh_command", "openocd", "forward_env", "path_mappings"},
        path,
        location,
    )
    host_id = _identifier(_required_string(item, "id", path, location), path, f"{location}.id")
    address = _required_string(item, "address", path, location)
    command = _string_array(
        item.get("ssh_command", ["ssh"]), path, f"{location}.ssh_command", nonempty=True
    )
    openocd = _absolute_remote(
        _required_string(item, "openocd", path, location), path, f"{location}.openocd"
    )
    forward_env = _string_array(item.get("forward_env", []), path, f"{location}.forward_env")
    if any(not _ENVIRONMENT_NAME.fullmatch(name) for name in forward_env):
        raise _error(path, f"{location}.forward_env", "contains an invalid environment name")
    _unique_names(list(forward_env), path, f"{location}.forward_env")
    mappings = _path_mappings(item.get("path_mappings", []), path, f"{location}.path_mappings")
    return InventoryHost(host_id, address, command, openocd, forward_env, mappings)


def _positive_number(value: Any, path: Path, location: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise _error(path, location, "expected a positive number")
    return float(value)


def _serial(raw: Any, path: Path, name: str) -> SerialEndpoint:
    location = f"serial.{name}"
    item = _table(raw, path, location)
    _keys(
        item,
        {
            "device",
            "baud",
            "data_bits",
            "parity",
            "stop_bits",
            "flow_control",
            "pattern",
            "timeout",
        },
        path,
        location,
    )
    device = _required_string(item, "device", path, location)
    baud = item.get("baud")
    if not isinstance(baud, int) or isinstance(baud, bool) or baud <= 0:
        raise _error(path, f"{location}.baud", "expected a positive integer")
    data_bits = item.get("data_bits", 8)
    if (
        not isinstance(data_bits, int)
        or isinstance(data_bits, bool)
        or data_bits not in {5, 6, 7, 8}
    ):
        raise _error(path, f"{location}.data_bits", "expected an integer from 5 through 8")
    parity = item.get("parity", "none")
    if parity not in {"none", "even", "odd"}:
        raise _error(path, f"{location}.parity", "expected none, even, or odd")
    stop_bits = item.get("stop_bits", 1)
    if not isinstance(stop_bits, int) or isinstance(stop_bits, bool) or stop_bits not in {1, 2}:
        raise _error(path, f"{location}.stop_bits", "expected 1 or 2")
    flow = item.get("flow_control", "none")
    if flow not in {"none", "hardware", "software"}:
        raise _error(path, f"{location}.flow_control", "expected none, hardware, or software")
    pattern = _required_string(item, "pattern", path, location)
    timeout = _positive_number(item.get("timeout"), path, f"{location}.timeout")
    return SerialEndpoint(name, device, baud, data_bits, parity, stop_bits, flow, pattern, timeout)


def _expectations(raw: Any, path: Path, location: str) -> Expectations:
    item = _table(raw if raw is not None else {}, path, location)
    _keys(item, {"patterns", "thread_info_pattern"}, path, location)
    patterns = _string_array(item.get("patterns", []), path, f"{location}.patterns")
    thread = _optional_string(item, "thread_info_pattern", path, location)
    return Expectations(patterns, thread)


def _rtt(raw: Any, path: Path, location: str) -> RttExpectation:
    item = _table(raw, path, location)
    _keys(item, {"port", "response", "input", "timeout"}, path, location)
    port = item.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise _error(path, f"{location}.port", "expected an integer TCP port from 1 through 65535")
    response = _required_string(item, "response", path, location)
    input_value = item.get("input", "")
    if not isinstance(input_value, str):
        raise _error(path, f"{location}.input", "expected a string")
    timeout = _positive_number(item.get("timeout"), path, f"{location}.timeout")
    return RttExpectation(port, response, input_value, timeout)


def _semihosting(raw: Any, path: Path, location: str) -> SemihostingExpectation:
    item = _table(raw, path, location)
    _keys(item, {"commands", "gdb_commands", "output", "timeout"}, path, location)
    commands = _string_array(item.get("commands"), path, f"{location}.commands")
    gdb_commands = _string_array(item.get("gdb_commands", []), path, f"{location}.gdb_commands")
    output = _required_string(item, "output", path, location)
    timeout = _positive_number(item.get("timeout"), path, f"{location}.timeout")
    return SemihostingExpectation(commands, gdb_commands, output, timeout)


def _profile(
    raw: Any,
    path: Path,
    name: str,
    host: InventoryHost,
    builds: dict[str, BuildRecipe],
    serial: dict[str, SerialEndpoint],
) -> OperationProfile:
    location = f"profiles.{name}"
    item = _table(raw, path, location)
    _keys(
        item,
        {
            "capabilities",
            "build",
            "serial",
            "probe_serial",
            "runner_args",
            "environment",
            "expect",
            "rtt",
            "semihosting",
        },
        path,
        location,
    )
    capabilities = _string_array(
        item.get("capabilities"), path, f"{location}.capabilities", nonempty=True
    )
    if any(capability not in _CAPABILITIES for capability in capabilities):
        raise _error(path, f"{location}.capabilities", "contains an unsupported capability")
    _unique_names(list(capabilities), path, f"{location}.capabilities")
    build_name = _required_string(item, "build", path, location)
    if build_name not in builds:
        raise _error(path, f"{location}.build", f"references unknown build {build_name!r}")
    serial_name = _optional_string(item, "serial", path, location)
    if serial_name is not None and serial_name not in serial:
        raise _error(
            path, f"{location}.serial", f"references unknown serial endpoint {serial_name!r}"
        )
    probe_serial = _optional_string(item, "probe_serial", path, location)
    runner_args = _string_array(item.get("runner_args", []), path, f"{location}.runner_args")
    raw_environment = item.get("environment", {})
    environment = _table(raw_environment, path, f"{location}.environment")
    for key, value in environment.items():
        if not _ENVIRONMENT_NAME.fullmatch(key):
            raise _error(path, f"{location}.environment", f"invalid environment name {key!r}")
        if key not in host.forward_env:
            raise _error(
                path, f"{location}.environment.{key}", "is not in the host forward_env allow-list"
            )
        if not isinstance(value, str) or "\0" in value:
            raise _error(path, f"{location}.environment.{key}", "expected a string value")
    expectations = _expectations(item.get("expect"), path, f"{location}.expect")
    raw_rtt = item.get("rtt")
    rtt = _rtt(raw_rtt, path, f"{location}.rtt") if raw_rtt is not None else None
    raw_semihosting = item.get("semihosting")
    semihosting = (
        _semihosting(raw_semihosting, path, f"{location}.semihosting")
        if raw_semihosting is not None
        else None
    )
    if "rtt" in capabilities and rtt is None:
        raise _error(path, location, "rtt capability requires an rtt table")
    if "semihosting" in capabilities and semihosting is None:
        raise _error(path, location, "semihosting capability requires a semihosting table")
    return OperationProfile(
        name,
        capabilities,
        build_name,
        serial_name,
        probe_serial,
        runner_args,
        tuple(sorted(environment.items())),
        expectations,
        rtt,
        semihosting,
    )


def _build(raw: Any, path: Path, name: str, target_board: str | None) -> BuildRecipe:
    location = f"builds.{name}"
    item = _table(raw, path, location)
    _keys(item, {"application", "board", "west_args", "cmake_args"}, path, location)
    application = _required_string(item, "application", path, location)
    if not Path(application).is_absolute() and any(
        part == ".." for part in Path(application).parts
    ):
        raise _error(path, f"{location}.application", "relative paths may not escape Zephyr tree")
    board = _optional_string(item, "board", path, location) or target_board
    if board is None:
        raise _error(path, f"{location}.board", "is required when target.board is absent")
    return BuildRecipe(
        name,
        application,
        board,
        _string_array(item.get("west_args", []), path, f"{location}.west_args"),
        _string_array(item.get("cmake_args", []), path, f"{location}.cmake_args"),
    )


def _target(raw: Any, path: Path, index: int, hosts: dict[str, InventoryHost]) -> InventoryTarget:
    location = f"targets[{index}]"
    item = _table(raw, path, location)
    _keys(
        item,
        {"id", "host", "zephyr_base", "west", "gdb", "board", "builds", "serial", "profiles"},
        path,
        location,
    )
    target_id = _identifier(_required_string(item, "id", path, location), path, f"{location}.id")
    host_name = _required_string(item, "host", path, location)
    if host_name not in hosts:
        raise _error(path, f"{location}.host", f"references unknown host {host_name!r}")
    zephyr_base = _local_path(
        _required_string(item, "zephyr_base", path, location), path, f"{location}.zephyr_base"
    )
    west = _local_path(_required_string(item, "west", path, location), path, f"{location}.west")
    gdb_text = _optional_string(item, "gdb", path, location)
    gdb = _local_path(gdb_text, path, f"{location}.gdb") if gdb_text else None
    board = _optional_string(item, "board", path, location)
    raw_builds = _table(item.get("builds"), path, f"{location}.builds")
    if not raw_builds:
        raise _error(path, f"{location}.builds", "must contain at least one named recipe")
    builds = {name: _build(raw, path, name, board) for name, raw in raw_builds.items()}
    _unique_names(list(builds), path, f"{location}.builds")
    raw_serial = _table(item.get("serial", {}), path, f"{location}.serial")
    serial = {name: _serial(raw, path, name) for name, raw in raw_serial.items()}
    _unique_names(list(serial), path, f"{location}.serial")
    host = hosts[host_name]
    raw_profiles = _table(item.get("profiles"), path, f"{location}.profiles")
    if not raw_profiles:
        raise _error(path, f"{location}.profiles", "must contain at least one named profile")
    profiles = tuple(
        _profile(raw, path, name, host, builds, serial) for name, raw in raw_profiles.items()
    )
    return InventoryTarget(
        target_id,
        host_name,
        zephyr_base,
        west,
        gdb,
        board,
        tuple(builds.values()),
        tuple(serial.values()),
        profiles,
    )


def load_inventory(path: Path | str) -> Inventory:
    """Load and strictly validate an external inventory TOML file."""
    inventory_path = Path(path).expanduser().resolve()
    try:
        with inventory_path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise InventoryError(f"cannot read inventory {inventory_path}: {error}") from error
    if not isinstance(document, dict):
        raise InventoryError(f"invalid inventory {inventory_path}: expected a TOML table")
    _keys(document, {"schema_version", "hosts", "targets"}, inventory_path, "inventory")
    if document.get("schema_version") != 1:
        raise _error(inventory_path, "schema_version", "expected integer 1")
    raw_hosts = document.get("hosts")
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise _error(inventory_path, "hosts", "expected a non-empty array of tables")
    hosts_list = [_host(raw, inventory_path, index) for index, raw in enumerate(raw_hosts)]
    _unique_names([host.id for host in hosts_list], inventory_path, "hosts")
    hosts = {host.id: host for host in hosts_list}
    raw_targets = document.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise _error(inventory_path, "targets", "expected a non-empty array of tables")
    targets_list = [
        _target(raw, inventory_path, index, hosts) for index, raw in enumerate(raw_targets)
    ]
    _unique_names([target.id for target in targets_list], inventory_path, "targets")
    return Inventory(inventory_path, 1, tuple(hosts_list), tuple(targets_list))


def render_product_config(host: InventoryHost, *, default: str = "local") -> str:
    """Render a frozen product config from one inventory host."""
    if default not in {"local", "remote"}:
        raise ValueError("default must be local or remote")

    def toml_string(value: str) -> str:
        return json.dumps(value)

    lines = [
        "[zephyr]",
        f"default = {toml_string(default)}",
        "",
        "[remote]",
        f"host = {toml_string(host.address)}",
        f"openocd = {toml_string(host.openocd)}",
        "",
        "[ssh]",
        "command = [" + ", ".join(toml_string(item) for item in host.ssh_command) + "]",
        "",
        "[openocd]",
        "forward_env = [" + ", ".join(toml_string(item) for item in host.forward_env) + "]",
    ]
    for mapping in host.path_mappings:
        lines.extend(
            (
                "",
                "[[paths.map]]",
                f"local = {toml_string(str(mapping.local))}",
                f"remote = {toml_string(str(mapping.remote))}",
            )
        )
    return "\n".join(lines) + "\n"


def inventory_path_from_environment() -> Path | None:
    """Return the fallback inventory path, if configured."""
    value = os.environ.get("ZRO_HARDWARE_CONFIG")
    return Path(value).expanduser().resolve() if value else None
