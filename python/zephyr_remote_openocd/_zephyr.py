# SPDX-License-Identifier: Apache-2.0

"""Select a concrete adapter from west's active Zephyr source."""

from __future__ import annotations

import argparse
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runners.core import RunnerConfig, ZephyrBinaryRunner


class UnsupportedZephyrError(ValueError):
    """The selected source has no supported stable runner adapter."""


def adapter_module(version_file: Path) -> str:
    """Read source VERSION without consulting generated build state or Git."""
    support = "supported versions are stable Zephyr 4.4.x releases"
    try:
        fields = dict(
            line.split("=", 1) for line in version_file.read_text().splitlines() if line.strip()
        )
        fields = {key.strip(): value.strip() for key, value in fields.items()}
        major, minor, patch, tweak = (
            int(fields[key])
            for key in ("VERSION_MAJOR", "VERSION_MINOR", "PATCHLEVEL", "VERSION_TWEAK")
        )
        extra = fields["EXTRAVERSION"]
        if any(value < 0 or value > 255 for value in (major, minor, patch, tweak)):
            raise ValueError("version fields must be in 0..255")
    except (OSError, UnicodeError, ValueError, KeyError) as error:
        raise UnsupportedZephyrError(
            f"cannot read Zephyr version from {version_file}: {error}; {support}"
        ) from error
    version = (
        f"{major}.{minor}.{patch}" + (f"+{tweak}" if tweak else "") + (f"-{extra}" if extra else "")
    )
    if (major, minor) != (4, 4) or patch == 99 or tweak or extra:
        raise UnsupportedZephyrError(f"unsupported Zephyr {version} in {version_file}; {support}")
    return f"zephyr_remote_openocd.zephyr{major}{minor}.runner"


def select_runner() -> type[ZephyrBinaryRunner]:
    """Register only the active adapter, deferring unsupported-source errors."""
    try:
        zephyr_base = import_module("zephyr_ext_common").ZEPHYR_BASE
    except (ImportError, AttributeError) as error:
        return _unavailable_runner(
            "cannot determine active Zephyr source: "
            f"zephyr_ext_common.ZEPHYR_BASE is unavailable: {error}"
        )

    try:
        module = adapter_module(Path(zephyr_base) / "VERSION")
    except UnsupportedZephyrError as error:
        return _unavailable_runner(str(error))
    return import_module(module).RemoteOpenOcdBinaryRunner


def _unavailable_runner(diagnostic: str) -> type[ZephyrBinaryRunner]:
    # All activated external runners are imported, even for -r openocd. Keep
    # that import harmless and fail only when west asks for this runner's parser.
    from runners.core import (  # pylint: disable=import-outside-toplevel,no-name-in-module
        RunnerCaps,
        ZephyrBinaryRunner,
    )

    class UnavailableRemoteOpenOcdBinaryRunner(ZephyrBinaryRunner):
        """Keep stock runners usable when this source has no remote adapter."""

        @classmethod
        def name(cls) -> str:
            return "remote_openocd"

        @classmethod
        def capabilities(cls) -> RunnerCaps:
            return RunnerCaps(commands={"flash", "debug", "debugserver", "attach", "rtt"})

        @classmethod
        def do_add_parser(cls, parser: argparse.ArgumentParser) -> None:
            parser.error(diagnostic)

        @classmethod
        def do_create(cls, cfg: RunnerConfig, args: argparse.Namespace) -> ZephyrBinaryRunner:
            raise RuntimeError(diagnostic)

        def do_run(self, command: str, **kwargs: object) -> None:
            raise RuntimeError(diagnostic)

    return UnavailableRemoteOpenOcdBinaryRunner
