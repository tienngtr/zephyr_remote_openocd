# SPDX-License-Identifier: Apache-2.0

"""Strict YAML inventory for external hardware validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator


class InventoryError(ValueError):
    """An actionable inventory validation error."""


class _StrictLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate and non-string mapping keys."""

    def construct_mapping(self, node, deep: bool = False):
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, "expected a mapping", node.start_mark
            )
        result: dict[str, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "mapping keys must be strings",
                    key_node.start_mark,
                )
            if key in result:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


@dataclass(frozen=True)
class InventoryPathMapping:
    local: Path
    remote: PurePosixPath


@dataclass(frozen=True)
class BuildEnvironment:
    name: str
    zephyr_base: Path
    west: Path


@dataclass(frozen=True)
class Toolchain:
    name: str
    gdb: Path


@dataclass(frozen=True)
class InventoryHost:
    name: str
    ssh_host: str
    openocd_command: tuple[str, ...]
    ssh_command: tuple[str, ...]
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


@dataclass(frozen=True)
class SerialExpectation:
    endpoint: str
    pattern: str
    timeout: float


@dataclass(frozen=True)
class FlashOperation:
    precondition_build: str
    quiescence_timeout: float
    serial: SerialExpectation
    output_patterns: tuple[str, ...]
    assert_bindto: bool


@dataclass(frozen=True)
class DebugOperation:
    breakpoint: str
    output_patterns: tuple[str, ...]


@dataclass(frozen=True)
class AttachOperation:
    precondition_build: str


@dataclass(frozen=True)
class DebugServerOperation:
    pass


@dataclass(frozen=True)
class ThreadInfoOperation:
    pattern: str


@dataclass(frozen=True)
class RttOperation:
    port: int
    response: str
    input: str
    timeout: float
    program_survives_reset: bool
    breakpoint: str


@dataclass(frozen=True)
class SemihostingOperation:
    commands: tuple[str, ...]
    gdb_commands: tuple[str, ...]
    output: str
    timeout: float


type Operation = (
    FlashOperation
    | DebugOperation
    | AttachOperation
    | DebugServerOperation
    | ThreadInfoOperation
    | RttOperation
    | SemihostingOperation
)


@dataclass(frozen=True)
class OperationProfile:
    name: str
    build: str
    probe_serial: str | None
    runner_args: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    operations: Mapping[str, Operation]

    def operation(self, name: str) -> Operation:
        return self.operations[name]

    @property
    def operation_names(self) -> tuple[str, ...]:
        return tuple(self.operations)


@dataclass(frozen=True)
class InventoryTarget:
    name: str
    host: str
    build_environment: str
    toolchain: str | None
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

    def profile(self, name: str) -> OperationProfile:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        raise KeyError(name)


@dataclass(frozen=True)
class Inventory:
    path: Path
    build_environments: tuple[BuildEnvironment, ...]
    toolchains: tuple[Toolchain, ...]
    hosts: tuple[InventoryHost, ...]
    targets: tuple[InventoryTarget, ...]

    def build_environment(self, name: str) -> BuildEnvironment:
        for environment in self.build_environments:
            if environment.name == name:
                return environment
        raise KeyError(name)

    def toolchain(self, name: str) -> Toolchain:
        for toolchain in self.toolchains:
            if toolchain.name == name:
                return toolchain
        raise KeyError(name)

    def host(self, name: str) -> InventoryHost:
        for host in self.hosts:
            if host.name == name:
                return host
        raise KeyError(name)

    def target(self, name: str) -> InventoryTarget:
        for target in self.targets:
            if target.name == name:
                return target
        raise KeyError(name)


def _error(path: Path, location: str, message: str) -> InventoryError:
    return InventoryError(f"invalid {location} in {path}: {message}")


def _local_path(value: str, path: Path, location: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise _error(path, location, str(error)) from error


def _schema_path() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "hardware.schema.json"


def _load_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise InventoryError(f"cannot read inventory {path}: {error}") from error
    try:
        documents = list(yaml.load_all(text, Loader=_StrictLoader))
    except yaml.YAMLError as error:
        raise InventoryError(f"cannot read inventory {path}: {error}") from error
    if len(documents) != 1:
        raise InventoryError(f"invalid inventory in {path}: expected one YAML document")
    document = documents[0]
    if not isinstance(document, dict):
        raise InventoryError(f"invalid inventory in {path}: expected a YAML mapping")
    return cast(dict[str, Any], document)


def _validate_schema(document: dict[str, Any], path: Path) -> None:
    try:
        schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"cannot read hardware inventory schema: {error}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        schema_error = errors[0]
        location = ".".join(str(item) for item in schema_error.absolute_path) or "inventory"
        raise _error(path, location, schema_error.message)


def _path_mappings(
    values: Mapping[str, str], path: Path, location: str
) -> tuple[InventoryPathMapping, ...]:
    result: list[InventoryPathMapping] = []
    locals_seen: dict[Path, PurePosixPath] = {}
    for local_text, remote_text in values.items():
        local = _local_path(local_text, path, f"{location}.{local_text}")
        remote = PurePosixPath(remote_text)
        if local in locals_seen:
            kind = "duplicate" if locals_seen[local] == remote else "conflicting"
            raise _error(path, location, f"{kind} mapping for {local}")
        locals_seen[local] = remote
        result.append(InventoryPathMapping(local, remote))
    return tuple(result)


def _build_environment(name: str, raw: dict[str, Any], path: Path) -> BuildEnvironment:
    location = f"build_environments.{name}"
    return BuildEnvironment(
        name,
        _local_path(raw["zephyr_base"], path, f"{location}.zephyr_base"),
        _local_path(raw["west"], path, f"{location}.west"),
    )


def _toolchain(name: str, raw: dict[str, Any], path: Path) -> Toolchain:
    return Toolchain(name, _local_path(raw["gdb"], path, f"toolchains.{name}.gdb"))


def _host(name: str, raw: dict[str, Any], path: Path) -> InventoryHost:
    return InventoryHost(
        name,
        raw["ssh_host"],
        tuple(raw["openocd_command"]),
        tuple(raw.get("ssh_command", ["ssh"])),
        tuple(raw.get("forward_env", [])),
        _path_mappings(raw.get("path_mappings", {}), path, f"hosts.{name}.path_mappings"),
    )


def _serial(name: str, raw: dict[str, Any]) -> SerialEndpoint:
    return SerialEndpoint(
        name,
        raw["device"],
        raw["baud"],
        raw.get("data_bits", 8),
        raw.get("parity", "none"),
        raw.get("stop_bits", 1),
        raw.get("flow_control", "none"),
    )


def _precondition(
    value: str,
    builds: Mapping[str, BuildRecipe],
    selected: str,
    path: Path,
    location: str,
) -> str:
    if value not in builds:
        raise _error(path, location, f"references unknown build {value!r}")
    if value == selected:
        raise _error(path, location, "must differ from the profile build")
    return value


def _operation(
    name: str,
    raw: dict[str, Any],
    builds: Mapping[str, BuildRecipe],
    serial: Mapping[str, SerialEndpoint],
    selected_build: str,
    path: Path,
    location: str,
) -> Operation:
    if name == "flash":
        serial_raw = raw["serial"]
        endpoint = serial_raw["endpoint"]
        if endpoint not in serial:
            raise _error(
                path,
                f"{location}.serial.endpoint",
                f"references unknown serial {endpoint!r}",
            )
        return FlashOperation(
            _precondition(
                raw["precondition_build"],
                builds,
                selected_build,
                path,
                f"{location}.precondition_build",
            ),
            float(raw["quiescence_timeout"]),
            SerialExpectation(endpoint, serial_raw["pattern"], float(serial_raw["timeout"])),
            tuple(raw.get("output_patterns", [])),
            raw.get("assert_bindto", False),
        )
    if name == "debug":
        return DebugOperation(raw["breakpoint"], tuple(raw.get("output_patterns", [])))
    if name == "attach":
        return AttachOperation(
            _precondition(
                raw["precondition_build"],
                builds,
                selected_build,
                path,
                f"{location}.precondition_build",
            )
        )
    if name == "debugserver":
        return DebugServerOperation()
    if name == "thread_info":
        return ThreadInfoOperation(raw["pattern"])
    if name == "rtt":
        return RttOperation(
            raw["port"],
            raw["response"],
            raw.get("input", ""),
            float(raw["timeout"]),
            raw["program_survives_reset"],
            raw["breakpoint"],
        )
    if name == "semihosting":
        return SemihostingOperation(
            tuple(raw["commands"]),
            tuple(raw.get("gdb_commands", [])),
            raw["output"],
            float(raw["timeout"]),
        )
    raise AssertionError(f"schema allowed unsupported operation {name!r}")


def _profile(
    name: str,
    raw: dict[str, Any],
    host: InventoryHost,
    builds: Mapping[str, BuildRecipe],
    serial: Mapping[str, SerialEndpoint],
    path: Path,
) -> OperationProfile:
    location = f"profiles.{name}"
    selected_build = raw["build"]
    if selected_build not in builds:
        raise _error(path, f"{location}.build", f"references unknown build {selected_build!r}")
    environment = raw.get("environment", {})
    for key in environment:
        if key not in host.forward_env:
            raise _error(
                path,
                f"{location}.environment.{key}",
                "is not in the host forward_env allow-list",
            )
    operations = {
        operation_name: _operation(
            operation_name,
            operation_raw,
            builds,
            serial,
            selected_build,
            path,
            f"{location}.operations.{operation_name}",
        )
        for operation_name, operation_raw in raw["operations"].items()
    }
    return OperationProfile(
        name,
        selected_build,
        raw.get("probe_serial"),
        tuple(raw.get("runner_args", [])),
        tuple(sorted(environment.items())),
        MappingProxyType(operations),
    )


def _target(
    name: str,
    raw: dict[str, Any],
    hosts: Mapping[str, InventoryHost],
    build_environments: Mapping[str, BuildEnvironment],
    toolchains: Mapping[str, Toolchain],
    path: Path,
) -> InventoryTarget:
    location = f"targets.{name}"
    host_name, environment_name, toolchain_name = _target_references(
        raw, location, hosts, build_environments, toolchains, path
    )
    target_board = raw.get("board")
    builds = _target_builds(raw["builds"], target_board, location, path)
    serial = _target_serial(raw.get("serial", {}))
    host = hosts[host_name]
    profiles = _target_profiles(raw["profiles"], host, builds, serial, path)
    direct_gdb = any(
        operation in {"debugserver", "rtt"}
        for profile in profiles
        for operation in profile.operation_names
    )
    if direct_gdb and toolchain_name is None:
        raise _error(path, f"{location}.toolchain", "is required by debugserver and rtt")
    return InventoryTarget(
        name,
        host_name,
        environment_name,
        toolchain_name,
        target_board,
        tuple(builds.values()),
        tuple(serial.values()),
        profiles,
    )


def _target_references(raw, location, hosts, build_environments, toolchains, path):
    host_name = raw["host"]
    if host_name not in hosts:
        raise _error(path, f"{location}.host", f"references unknown host {host_name!r}")
    environment_name = raw["build_environment"]
    if environment_name not in build_environments:
        raise _error(
            path,
            f"{location}.build_environment",
            f"references unknown build environment {environment_name!r}",
        )
    toolchain_name = raw.get("toolchain")
    if toolchain_name is not None and toolchain_name not in toolchains:
        raise _error(
            path, f"{location}.toolchain", f"references unknown toolchain {toolchain_name!r}"
        )
    return host_name, environment_name, toolchain_name


def _target_builds(raw_builds, target_board, location, path):
    builds = {}
    for build_name, build_raw in raw_builds.items():
        application = build_raw["application"]
        if not Path(application).is_absolute() and ".." in Path(application).parts:
            raise _error(
                path,
                f"{location}.builds.{build_name}.application",
                "relative paths may not escape Zephyr tree",
            )
        board = build_raw.get("board", target_board)
        if board is None:
            raise _error(
                path,
                f"{location}.builds.{build_name}.board",
                "is required when target.board is absent",
            )
        builds[build_name] = BuildRecipe(
            build_name,
            application,
            board,
            tuple(build_raw.get("west_args", [])),
            tuple(build_raw.get("cmake_args", [])),
        )
    return builds


def _target_serial(raw_serial):
    return {
        endpoint_name: _serial(endpoint_name, endpoint_raw)
        for endpoint_name, endpoint_raw in raw_serial.items()
    }


def _target_profiles(raw_profiles, host, builds, serial, path):
    return tuple(
        _profile(profile_name, profile_raw, host, builds, serial, path)
        for profile_name, profile_raw in raw_profiles.items()
    )


def load_inventory(path: Path | str) -> Inventory:
    """Load and strictly validate an external inventory YAML file."""
    inventory_path = Path(path).expanduser().resolve()
    document = _load_document(inventory_path)
    _validate_schema(document, inventory_path)
    build_environments = {
        name: _build_environment(name, raw, inventory_path)
        for name, raw in document["build_environments"].items()
    }
    toolchains = {
        name: _toolchain(name, raw, inventory_path)
        for name, raw in document.get("toolchains", {}).items()
    }
    hosts = {name: _host(name, raw, inventory_path) for name, raw in document["hosts"].items()}
    targets = tuple(
        _target(name, raw, hosts, build_environments, toolchains, inventory_path)
        for name, raw in document["targets"].items()
    )
    return Inventory(
        inventory_path,
        tuple(build_environments.values()),
        tuple(toolchains.values()),
        tuple(hosts.values()),
        targets,
    )


def render_product_config(host: InventoryHost, *, default_runner: str = "openocd") -> str:
    """Render a product YAML config from one inventory host."""
    if default_runner not in {"openocd", "remote_openocd"}:
        raise ValueError("default_runner must be openocd or remote_openocd")
    remote = {
        "ssh_host": host.ssh_host,
        "openocd_command": list(host.openocd_command),
        "ssh_command": list(host.ssh_command),
        "forward_env": list(host.forward_env),
        "path_mappings": {
            str(mapping.local): str(mapping.remote) for mapping in host.path_mappings
        },
    }
    return yaml.safe_dump(
        {
            "default_runner": default_runner,
            "default_remote": host.name,
            "remotes": {host.name: remote},
        },
        sort_keys=False,
    )
