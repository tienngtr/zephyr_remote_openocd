# SPDX-License-Identifier: Apache-2.0

"""Prototype user configuration loading."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


class ConfigError(ValueError):
    """An actionable configuration error."""


@dataclass(frozen=True)
class PrototypeConfig:
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


def default_config_path() -> Path:
    override = os.environ.get("ZEPHYR_REMOTE_OPENOCD_CONFIG")
    return (
        Path(override).expanduser()
        if override
        else Path.home() / ".config" / "zephyr-remote-openocd" / "config.toml"
    )


def load_config(path: Path | None = None) -> PrototypeConfig:
    config_path = path or default_config_path()
    data: dict[str, object] = {}
    if config_path.exists():
        try:
            with config_path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"invalid configuration {config_path}: {error}") from error

    zephyr = data.get("zephyr", {})
    remote = data.get("remote", {})
    ssh = data.get("ssh", {})
    openocd_section = data.get("openocd", {})
    paths = data.get("paths", {})
    if not all(
        isinstance(section, dict) for section in (zephyr, remote, ssh, openocd_section, paths)
    ):
        raise ConfigError(f"invalid configuration {config_path}: sections must be TOML tables")

    selected = zephyr.get("default", "local")
    if selected not in {"local", "remote"}:
        raise ConfigError(f"invalid zephyr.default in {config_path}: expected 'local' or 'remote'")
    command = ssh.get("command", ["ssh"])
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(arg, str) and arg for arg in command)
    ):
        raise ConfigError(
            f"invalid ssh.command in {config_path}: expected a non-empty string array"
        )

    host = remote.get("host")
    openocd = remote.get("openocd")
    if host is not None and not isinstance(host, str):
        raise ConfigError(f"invalid remote.host in {config_path}: expected a string")
    if openocd is not None and not isinstance(openocd, str):
        raise ConfigError(f"invalid remote.openocd in {config_path}: expected a string")

    forward_env = openocd_section.get("forward_env", [])
    if not isinstance(forward_env, list) or not all(
        isinstance(name, str) and name and "=" not in name and "\0" not in name
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
        if not isinstance(item, dict) or set(item) != {"local", "remote"}:
            raise ConfigError(
                f"invalid paths.map[{index}] in {config_path}: expected local and remote strings"
            )
        local_value, remote_value = item["local"], item["remote"]
        if not isinstance(local_value, str) or not isinstance(remote_value, str):
            raise ConfigError(
                f"invalid paths.map[{index}] in {config_path}: expected local and remote strings"
            )
        local_path = Path(local_value).expanduser().resolve()
        remote_path = PurePosixPath(remote_value)
        if not remote_path.is_absolute() or any(
            part in ("", ".", "..") for part in remote_path.parts[1:]
        ):
            raise ConfigError(
                f"invalid paths.map[{index}].remote in {config_path}: "
                "expected a normalized absolute POSIX path"
            )
        previous = local_destinations.get(local_path)
        if previous is not None and previous != remote_path:
            raise ConfigError(f"conflicting mappings for {local_path} in {config_path}")
        if previous is None:
            mappings.append(PathMapping(local_path, remote_path))
            local_destinations[local_path] = remote_path

    return PrototypeConfig(
        config_path,
        selected,
        host,
        openocd,
        tuple(command),
        tuple(forward_env),
        tuple(mappings),
    )
