# SPDX-License-Identifier: Apache-2.0

"""User configuration loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


class ConfigError(ValueError):
    """An actionable configuration error."""


@dataclass(frozen=True)
class RemoteOpenOcdConfig:
    path: Path
    default: str
    remote_host: str | None
    remote_openocd: str | None
    ssh_command: tuple[str, ...]
    forward_env: tuple[str, ...] = ()
    path_mappings: tuple[PathMapping, ...] = ()

    def printable(self) -> dict[str, object]:
        result = asdict(self)
        result["path"] = str(self.path)
        result["ssh_command"] = list(self.ssh_command)
        result["forward_env"] = list(self.forward_env)
        result["path_mappings"] = [
            {"local": str(item.local), "remote": str(item.remote)} for item in self.path_mappings
        ]
        return result


@dataclass(frozen=True)
class PathMapping:
    local: Path
    remote: PurePosixPath


_SCHEMA_KEYS = {
    "zephyr": {"default"},
    "remote": {"host", "openocd"},
    "ssh": {"command"},
    "openocd": {"forward_env"},
    "paths": {"map"},
}


def _reject_unknown_keys(
    table: dict[str, object], allowed: set[str], location: str, config_path: Path
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        joined = ", ".join(f"{location}.{key}" if location else key for key in unknown)
        raise ConfigError(f"unknown configuration key in {config_path}: {joined}")


def _optional_nonempty_string(value: object, name: str, config_path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ConfigError(f"invalid {name} in {config_path}: expected a non-empty string")
    return value


def _absolute_remote_path(
    value: object, name: str, config_path: Path, *, allow_root: bool = False
) -> str | None:
    result = _optional_nonempty_string(value, name, config_path)
    if result is None:
        return None
    path = PurePosixPath(result)
    parts = result.split("/")
    if (
        not path.is_absolute()
        or result.startswith("//")
        or (result == "/" and not allow_root)
        or (result != "/" and any(part in ("", ".", "..") for part in parts[1:]))
    ):
        raise ConfigError(
            f"invalid {name} in {config_path}: expected a normalized absolute POSIX path"
        )
    return result


def default_config_path() -> Path:
    override = os.environ.get("ZEPHYR_REMOTE_OPENOCD_CONFIG")
    return (
        Path(override).expanduser()
        if override
        else Path.home() / ".config" / "zephyr-remote-openocd" / "config.toml"
    )


def load_config(path: Path | None = None) -> RemoteOpenOcdConfig:
    config_path = path or default_config_path()
    data: dict[str, object] = {}
    if os.path.lexists(config_path):
        try:
            with config_path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"invalid configuration {config_path}: {error}") from error

    _reject_unknown_keys(data, set(_SCHEMA_KEYS), "", config_path)

    zephyr = data.get("zephyr", {})
    remote = data.get("remote", {})
    ssh = data.get("ssh", {})
    openocd_section = data.get("openocd", {})
    paths = data.get("paths", {})
    if not all(
        isinstance(section, dict) for section in (zephyr, remote, ssh, openocd_section, paths)
    ):
        raise ConfigError(f"invalid configuration {config_path}: sections must be TOML tables")

    for name, section in (
        ("zephyr", zephyr),
        ("remote", remote),
        ("ssh", ssh),
        ("openocd", openocd_section),
        ("paths", paths),
    ):
        _reject_unknown_keys(section, _SCHEMA_KEYS[name], name, config_path)

    selected = zephyr.get("default", "local")
    if selected not in {"local", "remote"}:
        raise ConfigError(f"invalid zephyr.default in {config_path}: expected 'local' or 'remote'")
    command = ssh.get("command", ["ssh"])
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and arg.strip() and "\0" not in arg for arg in command)
    ):
        raise ConfigError(
            f"invalid ssh.command in {config_path}: expected a non-empty string array"
        )

    host = _optional_nonempty_string(remote.get("host"), "remote.host", config_path)
    openocd = _absolute_remote_path(remote.get("openocd"), "remote.openocd", config_path)

    forward_env = openocd_section.get("forward_env", [])
    if not isinstance(forward_env, list) or not all(
        isinstance(name, str) and bool(name.strip()) and "=" not in name and "\0" not in name
        for name in forward_env
    ):
        raise ConfigError(f"invalid openocd.forward_env in {config_path}: expected a string array")
    if len(forward_env) != len(set(forward_env)):
        raise ConfigError(f"invalid openocd.forward_env in {config_path}: names must be unique")

    raw_mappings = paths.get("map", [])
    if not isinstance(raw_mappings, list):
        raise ConfigError(f"invalid paths.map in {config_path}: expected an array of tables")
    mappings = []
    local_destinations: dict[Path, PurePosixPath] = {}
    for index, item in enumerate(raw_mappings):
        location = f"paths.map[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"invalid {location} in {config_path}: expected a table")
        _reject_unknown_keys(item, {"local", "remote"}, location, config_path)
        missing = sorted({"local", "remote"} - set(item))
        if missing:
            raise ConfigError(f"invalid {location} in {config_path}: missing {', '.join(missing)}")
        local_value, remote_value = item["local"], item["remote"]
        if not isinstance(local_value, str) or not local_value.strip() or "\0" in local_value:
            raise ConfigError(
                f"invalid paths.map[{index}].local in {config_path}: "
                "expected a non-empty absolute path"
            )
        try:
            expanded_local = Path(local_value).expanduser()
        except (OSError, RuntimeError) as error:
            raise ConfigError(
                f"invalid paths.map[{index}].local in {config_path}: {error}"
            ) from error
        if not expanded_local.is_absolute():
            raise ConfigError(
                f"invalid paths.map[{index}].local in {config_path}: expected an absolute path"
            )
        try:
            local_path = expanded_local.resolve()
        except OSError as error:
            raise ConfigError(
                f"invalid paths.map[{index}].local in {config_path}: {error}"
            ) from error
        remote_value = _absolute_remote_path(
            remote_value,
            f"paths.map[{index}].remote",
            config_path,
            allow_root=True,
        )
        assert remote_value is not None
        remote_path = PurePosixPath(remote_value)
        previous = local_destinations.get(local_path)
        if previous is not None:
            kind = "duplicate" if previous == remote_path else "conflicting"
            raise ConfigError(f"{kind} mappings for {local_path} in {config_path}")
        mappings.append(PathMapping(local_path, remote_path))
        local_destinations[local_path] = remote_path

    return RemoteOpenOcdConfig(
        config_path,
        selected,
        host,
        openocd,
        tuple(command),
        tuple(forward_env),
        tuple(mappings),
    )


def require_remote_settings(config: RemoteOpenOcdConfig, operation: str) -> tuple[str, str]:
    """Return mandatory production settings or raise an actionable error."""
    if not config.remote_host:
        raise ConfigError(f"remote.host is required for remote {operation} ({config.path})")
    if not config.remote_openocd:
        raise ConfigError(f"remote.openocd is required for remote {operation} ({config.path})")
    return config.remote_host, config.remote_openocd
