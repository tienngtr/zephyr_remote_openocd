# SPDX-License-Identifier: Apache-2.0

"""Shared construction of the invariant portion of OpenOCD commands."""

from __future__ import annotations

from pathlib import Path

from .paths import ADDRESS_TOKEN, PathPlanner


def executable_argv(executable: str | tuple[str, ...]) -> list[str]:
    """Return a mutable argv prefix while preserving multi-word executables."""

    return list((executable,) if isinstance(executable, str) else executable)


def plan_support_paths(
    search_paths: tuple[str, ...], config_files: tuple[str, ...], planner: PathPlanner
) -> tuple[list[str], list[str]]:
    """Plan search/config paths in Zephyr's stable indexed order."""

    indexed_search = list(enumerate(search_paths))
    planned_search: dict[int, str] = {}
    for index, path in sorted(indexed_search, key=lambda item: len(Path(item[1]).resolve().parts)):
        planned_search[index] = planner.plan_directory(Path(path), f"search_{index}").remote
    remote_search = [planned_search[index] for index, _ in indexed_search]
    remote_configs = [
        planner.plan_file(Path(path), f"config-{index}").remote
        for index, path in enumerate(config_files)
    ]
    return remote_search, remote_configs


def base_argv(
    executable: str | tuple[str, ...],
    serial: str | None,
    remote_search: list[str],
    remote_configs: list[str],
) -> list[str]:
    """Build the shared serial, search-path, config, and loopback prefix."""

    argv = executable_argv(executable)
    if serial:
        # Board configurations may consume this variable while they load.
        argv.extend(("-c", "set _ZEPHYR_BOARD_SERIAL " + serial))
    for path in remote_search:
        argv.extend(("-s", path))
    for path in remote_configs:
        argv.extend(("-f", path))
    argv.extend(("-c", f"bindto {ADDRESS_TOKEN}"))
    return argv
