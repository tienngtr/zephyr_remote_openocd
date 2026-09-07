# SPDX-License-Identifier: Apache-2.0

"""YAML user configuration loading and remote resolution."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

try:
    import yaml
    from jsonschema import Draft202012Validator
except ImportError as error:  # pragma: no cover - setup diagnostics
    yaml = None
    Draft202012Validator = None
    _IMPORT_ERROR = error
else:
    _IMPORT_ERROR = None


class ConfigError(ValueError):
    """An actionable configuration error."""


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COMMAND_EXECUTABLE = re.compile(r"^(?:[^/]+|/[^/].*|~/.*|~)$")
_LOCAL_PATH = re.compile(r"^(?:/.*|~(?:/.*|))$")


@dataclass(frozen=True)
class PathMapping:
    local: Path
    remote: PurePosixPath


@dataclass(frozen=True)
class Preset:
    openocd_command: tuple[str, ...] | None = None
    ssh_command: tuple[str, ...] | None = None
    forward_env: tuple[str, ...] | None = None
    path_mappings: tuple[PathMapping, ...] | None = None


@dataclass(frozen=True)
class RemoteDefinition:
    preset: str | None = None
    ssh_host: str | None = None
    openocd_command: tuple[str, ...] | None = None
    ssh_command: tuple[str, ...] | None = None
    forward_env: tuple[str, ...] | None = None
    path_mappings: tuple[PathMapping, ...] | None = None


@dataclass(frozen=True)
class ResolvedRemote:
    name: str
    path: Path
    ssh_host: str
    openocd_command: tuple[str, ...]
    ssh_command: tuple[str, ...]
    forward_env: tuple[str, ...]
    path_mappings: tuple[PathMapping, ...]

    @property
    def remote_host(self) -> str:
        return self.ssh_host

    @property
    def remote_openocd(self) -> str:
        return self.openocd_command[0] if self.openocd_command else ""

    def printable(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "name": self.name,
            "ssh_host": self.ssh_host,
            "openocd_command": list(self.openocd_command),
            "ssh_command": list(self.ssh_command),
            "forward_env": list(self.forward_env),
            "path_mappings": [
                {"local": str(item.local), "remote": str(item.remote)}
                for item in self.path_mappings
            ],
        }


@dataclass(frozen=True)
class RemoteOpenOcdConfig:
    """Parsed YAML document; remotes resolve only when selected."""

    path: Path
    default_runner: str
    default_remote: str | None
    presets: Mapping[str, Preset]
    remotes: Mapping[str, RemoteDefinition]

    def printable(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "default_runner": self.default_runner,
            "default_remote": self.default_remote,
            "presets": sorted(self.presets),
            "remotes": sorted(self.remotes),
        }


def _schema_path() -> Path:
    return (
        Path(__file__).resolve().parents[2] / "docs" / "requirements" / "configuration.schema.json"
    )


def _require_dependencies(config_path: Path) -> None:
    if _IMPORT_ERROR is not None:
        raise ConfigError(
            f"cannot load YAML configuration {config_path}: install PyYAML and jsonschema"
        ) from _IMPORT_ERROR


class _StrictLoader(yaml.SafeLoader if yaml is not None else object):
    """SafeLoader that rejects duplicate and non-string mapping keys."""

    def construct_mapping(self, node, deep: bool = False):  # type: ignore[no-untyped-def]
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


def default_config_path() -> Path:
    override = os.environ.get("ZEPHYR_REMOTE_OPENOCD_CONFIG")
    return (
        Path(override).expanduser()
        if override
        else Path.home() / ".config" / "zephyr_remote_openocd" / "config.yaml"
    )


def _load_yaml(config_path: Path) -> dict[str, object]:
    _require_dependencies(config_path)
    try:
        text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except OSError as error:
        raise ConfigError(f"cannot read configuration {config_path}: {error}") from error
    if not text.strip():
        return {}
    try:
        documents = list(yaml.load_all(text, Loader=_StrictLoader))
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML configuration {config_path}: {error}") from error
    if not documents:
        return {}
    if len(documents) != 1:
        raise ConfigError(f"invalid YAML configuration {config_path}: expected one document")
    document = documents[0]
    if document is None:
        raise ConfigError(f"invalid YAML configuration {config_path}: root null is not allowed")
    if not isinstance(document, dict):
        raise ConfigError(f"invalid YAML configuration {config_path}: root must be a mapping")
    return document


def _validate_schema(document: dict[str, object], config_path: Path) -> None:
    try:
        schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read configuration schema: {error}") from error
    error = next(Draft202012Validator(schema).iter_errors(document), None)
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path)
        suffix = f" at {location}" if location else ""
        raise ConfigError(f"invalid configuration {config_path}{suffix}: {error.message}")


def _as_command(value: object, location: str, config_path: Path) -> tuple[str, ...] | None:
    if value is None:
        return None
    assert isinstance(value, list)
    command = tuple(value)
    if not _COMMAND_EXECUTABLE.fullmatch(command[0]) or (
        command[0].startswith("~") and command[0] not in {"~"} and not command[0].startswith("~/")
    ):
        raise ConfigError(
            f"invalid configuration {config_path} at {location}[0]: invalid executable"
        )
    if any("\0" in arg for arg in command):
        raise ConfigError(f"invalid configuration {config_path} at {location}: NUL is not allowed")
    return command


def _path_mappings(
    value: object, location: str, config_path: Path
) -> tuple[PathMapping, ...] | None:
    if value is None:
        return None
    assert isinstance(value, dict)
    mappings: list[PathMapping] = []
    seen: dict[Path, PurePosixPath] = {}
    for local_value, remote_value in value.items():
        if not _LOCAL_PATH.fullmatch(local_value) or not _valid_remote_path(remote_value):
            raise ConfigError(
                f"invalid configuration {config_path} at {location}: "
                "paths must be absolute or ~/path"
            )
        try:
            local = Path(local_value).expanduser().resolve()
        except (OSError, RuntimeError) as error:
            raise ConfigError(
                f"invalid configuration {config_path} at {location}: {error}"
            ) from error
        remote = PurePosixPath(remote_value)
        previous = seen.get(local)
        if previous is not None:
            kind = "duplicate" if previous == remote else "conflicting"
            raise ConfigError(f"{kind} mappings for {local} in {config_path}")
        seen[local] = remote
        mappings.append(PathMapping(local, remote))
    return tuple(mappings)


def _valid_remote_path(value: str) -> bool:
    """Remote paths are normalized POSIX paths or the current user's ``~``."""
    if value == "~":
        return True
    if value.startswith("~/"):
        parts = value[2:].split("/")
    elif value.startswith("/") and not value.startswith("//"):
        if value == "/":
            return True
        parts = value[1:].split("/")
    else:
        return False
    return all(part not in {"", ".", ".."} for part in parts)


def _definition(raw: dict[str, object], config_path: Path, location: str, *, remote: bool):
    kwargs: dict[str, object] = {}
    if remote:
        kwargs["preset"] = raw.get("preset")
        kwargs["ssh_host"] = raw.get("ssh_host")
    for name in ("openocd_command", "ssh_command", "forward_env", "path_mappings"):
        if name not in raw:
            continue
        value = raw[name]
        if name.endswith("command"):
            kwargs[name] = _as_command(value, f"{location}.{name}", config_path)
        elif name == "forward_env":
            kwargs[name] = tuple(value)
        else:
            kwargs[name] = _path_mappings(value, f"{location}.{name}", config_path)
    return RemoteDefinition(**kwargs) if remote else Preset(**kwargs)


def load_config(path: Path | None = None) -> RemoteOpenOcdConfig:
    config_path = (path or default_config_path()).expanduser()
    document = _load_yaml(config_path)
    _validate_schema(document, config_path)
    default_runner = document.get("default_runner", "openocd")
    default_remote = document.get("default_remote")
    presets_raw = document.get("presets", {})
    remotes_raw = document.get("remotes", {})
    assert isinstance(default_runner, str)
    assert default_remote is None or isinstance(default_remote, str)
    assert isinstance(presets_raw, dict) and isinstance(remotes_raw, dict)
    presets = {
        name: _definition(raw, config_path, f"presets.{name}", remote=False)
        for name, raw in presets_raw.items()
    }
    remotes = {
        name: _definition(raw, config_path, f"remotes.{name}", remote=True)
        for name, raw in remotes_raw.items()
    }
    return RemoteOpenOcdConfig(
        config_path,
        default_runner,
        default_remote,
        MappingProxyType(presets),
        MappingProxyType(remotes),
    )


def _merge_settings(preset: Preset | None, remote: RemoteDefinition) -> dict[str, object]:
    values: dict[str, object] = {
        "openocd_command": preset.openocd_command if preset else None,
        "ssh_command": preset.ssh_command if preset else None,
        "forward_env": preset.forward_env if preset else None,
        "path_mappings": preset.path_mappings if preset else None,
    }
    for name in values:
        value = getattr(remote, name)
        if value is not None:
            values[name] = value
    return values


def resolve_remote(
    config: RemoteOpenOcdConfig,
    remote_name: str | None = None,
    *,
    require_openocd: bool = True,
) -> ResolvedRemote:
    if remote_name is not None:
        selected_name = remote_name
    else:
        selected_name = os.environ.get("ZEPHYR_REMOTE_OPENOCD_REMOTE") or config.default_remote
    if not selected_name:
        raise ConfigError(
            "no remote selected for remote_openocd; use --remote or set "
            f"default_remote ({config.path})"
        )
    if not _IDENTIFIER.fullmatch(selected_name):
        raise ConfigError(f"invalid remote name {selected_name!r} in {config.path}")
    definition = config.remotes.get(selected_name)
    if definition is None:
        raise ConfigError(f"selected remote {selected_name!r} does not exist in {config.path}")
    preset = None
    if definition.preset is not None:
        preset = config.presets.get(definition.preset)
        if preset is None:
            raise ConfigError(
                f"remote {selected_name!r} references missing preset "
                f"{definition.preset!r} in {config.path}"
            )
    values = _merge_settings(preset, definition)
    command = values["openocd_command"]
    if command is None and require_openocd:
        raise ConfigError(
            f"openocd_command is required for remote {selected_name!r} ({config.path})"
        )
    if command is None:
        command = ()
    ssh_command = values["ssh_command"] or ("ssh",)
    if ssh_command[0] == "~" or ssh_command[0].startswith("~/"):
        ssh_command = (str(Path(ssh_command[0]).expanduser()), *ssh_command[1:])
    return ResolvedRemote(
        selected_name,
        config.path,
        definition.ssh_host or selected_name,
        command,
        ssh_command,
        values["forward_env"] or (),
        values["path_mappings"] or (),
    )


def require_remote_settings(config: ResolvedRemote, operation: str) -> tuple[str, str]:
    """Return mandatory production settings or raise an actionable error."""
    if not config.ssh_host:
        raise ConfigError(f"ssh_host is required for remote {operation} ({config.path})")
    if not config.openocd_command:
        raise ConfigError(f"openocd_command is required for remote {operation} ({config.path})")
    return config.ssh_host, config.openocd_command[0]
