"""Prototype user configuration loading."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """An actionable configuration error."""


@dataclass(frozen=True)
class PrototypeConfig:
    path: Path
    default: str
    remote_host: str | None
    remote_openocd: str | None
    ssh_command: tuple[str, ...]

    def printable(self) -> dict[str, object]:
        result = asdict(self)
        result["path"] = str(self.path)
        result["ssh_command"] = list(self.ssh_command)
        return result


def default_config_path() -> Path:
    override = os.environ.get("ZEPHYR_REMOTE_OPENOCD_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".config" / "zephyr-remote-openocd" / "config.toml"


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
    if not isinstance(zephyr, dict) or not isinstance(remote, dict) or not isinstance(ssh, dict):
        raise ConfigError(f"invalid configuration {config_path}: sections must be TOML tables")

    selected = zephyr.get("default", "local")
    if selected not in {"local", "remote"}:
        raise ConfigError(f"invalid zephyr.default in {config_path}: expected 'local' or 'remote'")
    command = ssh.get("command", ["ssh"])
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ConfigError(f"invalid ssh.command in {config_path}: expected a non-empty string array")

    host = remote.get("host")
    openocd = remote.get("openocd")
    if host is not None and not isinstance(host, str):
        raise ConfigError(f"invalid remote.host in {config_path}: expected a string")
    if openocd is not None and not isinstance(openocd, str):
        raise ConfigError(f"invalid remote.openocd in {config_path}: expected a string")

    return PrototypeConfig(config_path, selected, host, openocd, tuple(command))

