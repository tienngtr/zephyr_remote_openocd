# SPDX-License-Identifier: Apache-2.0

"""Import-boundary probes for a fresh isolated interpreter.

Run with ``python -I`` so neither the caller's environment nor this file's
directory affects imports. Arguments are ``PROBE PYTHON_ROOT`` followed by
probe arguments. The ``initializers`` probe imports every package initializer
and requires that no implementation module loads. The ``controller-facade``
probe imports the controller API while rejecting non-session dependencies and
import-time process, socket, or shell effects. ``config-without-dependency
MODULE CONFIG`` makes ``MODULE`` unimportable, then requires configuration
loading to import and report the missing dependency as ``ConfigError``.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

PACKAGES = ("controller", "openocd", "protocol", "helper", "zephyr44", "zephyr45")
FORBIDDEN_TOP_LEVEL = {"runners", "yaml", "jsonschema", "elftools"}
FORBIDDEN_PACKAGES = (
    "zephyr_remote_openocd.config",
    "zephyr_remote_openocd.helper",
    "zephyr_remote_openocd.openocd",
    "zephyr_remote_openocd.zephyr44",
    "zephyr_remote_openocd.zephyr45",
    "zephyr_remote_openocd._runner",
    "zephyr_remote_openocd._zephyr",
)
FORBIDDEN_EVENTS = {"subprocess.Popen", "socket.__new__", "os.system"}


def initializers() -> None:
    packages = {"zephyr_remote_openocd", *(f"zephyr_remote_openocd.{name}" for name in PACKAGES)}
    for name in sorted(packages):
        importlib.import_module(name)
    loaded = {name for name in sys.modules if name.startswith("zephyr_remote_openocd")}
    assert loaded == packages, loaded - packages


class ForbiddenImports:
    """Fail even attempted optional imports, not only what stays in sys.modules."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> None:
        if fullname.split(".")[0] in FORBIDDEN_TOP_LEVEL or any(
            fullname == name or fullname.startswith(name + ".") for name in FORBIDDEN_PACKAGES
        ):
            raise AssertionError(f"unexpected dependency: {fullname}")


class MissingModule:
    """Make one top-level package unimportable, as if it were not installed."""

    def __init__(self, name: str) -> None:
        self.name = name

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> None:
        if fullname.split(".")[0] == self.name:
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)


def reject_effect(event: str, _args: tuple[object, ...]) -> None:
    # Session imports may define effectful operations but must not execute them.
    if event in FORBIDDEN_EVENTS:
        raise AssertionError(f"import-time effect: {event}")


def controller_facade() -> None:
    sys.meta_path.insert(0, ForbiddenImports())
    sys.addaudithook(reject_effect)
    api = importlib.import_module("zephyr_remote_openocd.controller.api")
    assert api.SessionController is not None


def config_without_dependency(missing: str, config_path: str) -> None:
    sys.meta_path.insert(0, MissingModule(missing))
    config = importlib.import_module("zephyr_remote_openocd.config")
    try:
        config.load_config(Path(config_path))
    except config.ConfigError as error:
        assert isinstance(error.__cause__, ImportError), repr(error.__cause__)
        assert error.__cause__.name is not None
        assert error.__cause__.name.split(".")[0] == missing, error.__cause__.name
        assert "PyYAML" in str(error) and "jsonschema" in str(error), str(error)
    else:
        raise AssertionError(f"configuration loaded without {missing}")


def main() -> None:
    probe, python_root, *arguments = sys.argv[1:]
    sys.path.insert(0, python_root)
    if probe == "initializers":
        initializers()
    elif probe == "controller-facade":
        controller_facade()
    else:
        assert probe == "config-without-dependency"
        missing, config_path = arguments
        config_without_dependency(missing, config_path)


if __name__ == "__main__":
    main()
