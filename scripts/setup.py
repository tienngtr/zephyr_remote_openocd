#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Initialize Zephyr Remote OpenOCD's per-user configuration."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shlex
import sys
from collections.abc import Callable
from pathlib import Path


class SetupError(RuntimeError):
    """An actionable setup failure."""


def module_root(script_path: str | os.PathLike[str] | None = None) -> Path:
    """Return the module root independently of the current working directory."""
    path = Path(script_path) if script_path is not None else Path(__file__)
    return path.resolve().parent.parent


def config_path(home: str | os.PathLike[str] | None = None) -> Path:
    """Return the canonical per-user configuration path."""
    home_path = Path(home).expanduser() if home is not None else Path.home()
    return home_path / ".config" / "zephyr-remote-openocd" / "config.toml"


def pyelftools_available(
    finder: Callable[[str], object | None] = importlib.util.find_spec,
) -> bool:
    """Return whether the Zephyr environment can discover ``elftools``."""
    try:
        return finder("elftools") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _ensure_config_directory(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SetupError(f"cannot create configuration parent {path.parent}: {error}") from error
    if path.parent.exists() and not path.parent.is_dir():
        raise SetupError(f"configuration directory {path.parent} is not a directory")

    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise SetupError(f"configuration directory {path} is not a directory") from None
    except OSError as error:
        raise SetupError(f"cannot create configuration directory {path}: {error}") from error
    else:
        try:
            path.chmod(0o700)
        except OSError as error:
            raise SetupError(f"cannot set permissions on new directory {path}: {error}") from error


def initialize_config(root: Path, destination: Path) -> bool:
    """Create ``destination`` from the shipped template when absent."""
    template = root / "resources" / "config.toml.example"
    try:
        contents = template.read_bytes()
    except OSError as error:
        raise SetupError(
            f"cannot read shipped configuration template {template}: {error}"
        ) from error

    _ensure_config_directory(destination.parent)
    if os.path.lexists(destination):
        if not destination.is_file():
            raise SetupError(f"existing configuration path {destination} is not a regular file")
        return False

    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        if not destination.is_file():
            raise SetupError(
                f"existing configuration path {destination} is not a regular file"
            ) from None
        return False
    except OSError as error:
        raise SetupError(f"cannot create configuration file {destination}: {error}") from error

    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
        destination.chmod(0o600)
    except OSError as error:
        with contextlib.suppress(OSError):
            destination.unlink()
        raise SetupError(f"cannot write configuration file {destination}: {error}") from error
    return True


def _print_dependency_status(
    finder: Callable[[str], object | None] = importlib.util.find_spec,
) -> None:
    if pyelftools_available(finder):
        print("Python dependency:")
        print("  pyelftools: found")
    else:
        print("Warning: pyelftools is not available in this Python environment.")
        print("remote-openocd requires pyelftools for ELF inspection.")
        print("Use the Python environment configured for Zephyr.")


def main() -> int:
    try:
        root = module_root()
        destination = config_path()
        created = initialize_config(root, destination)
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    status = "created" if created else "already exists"
    print(f"Configuration ({status}): {destination}")
    print(f"Module root: {root}")
    print("Activate remote-openocd by making this path available to Zephyr:")
    print(f"  export EXTRA_ZEPHYR_MODULES={shlex.quote(str(root))}")
    _print_dependency_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
