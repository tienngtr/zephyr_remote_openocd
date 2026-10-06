# SPDX-License-Identifier: Apache-2.0

"""Shared construction of the invariant portion of OpenOCD commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .arguments import ArgumentTemplate
from .paths import PathPlanner, PlannedPath


@dataclass(frozen=True)
class OpenOcdBasePlan:
    """Immutable command prefix shared by flash and session debug plans."""

    argv: tuple[str, ...]
    literal_prefix: int
    argv_templates: tuple[tuple[int, ArgumentTemplate], ...]


def executable_argv(executable: str | tuple[str, ...]) -> list[str]:
    """Preserve the executable and opaque advanced fixed arguments in order."""

    return list((executable,) if isinstance(executable, str) else executable)


def _resolve_config_file(path: str, search_paths: tuple[str, ...]) -> Path:
    """Prefer direct files, then look up relative configs in supplied search order."""

    source = Path(path).expanduser()
    if source.is_absolute() or source.is_file():
        return source
    for directory in search_paths:
        candidate = Path(directory).expanduser() / source
        if candidate.is_file():
            return candidate
    # Keep missing-file reporting at the ordinary file-planning boundary.
    return source


def plan_support_paths(
    search_paths: tuple[str, ...], config_files: tuple[str, ...], planner: PathPlanner
) -> tuple[list[PlannedPath], list[PlannedPath]]:
    """Plan search/config paths in Zephyr's stable indexed order."""

    indexed_search = list(enumerate(search_paths))
    planned_search: dict[int, PlannedPath] = {}
    for index, path in sorted(indexed_search, key=lambda item: len(Path(item[1]).resolve().parts)):
        planned_search[index] = planner.plan_directory(Path(path), f"search_{index}")
    remote_search = [planned_search[index] for index, _ in indexed_search]
    remote_configs = [
        planner.plan_file(_resolve_config_file(path, search_paths), f"config-{index}")
        for index, path in enumerate(config_files)
    ]
    return remote_search, remote_configs


def base_argv(
    executable: str | tuple[str, ...],
    serial: str | None,
    remote_search: list[PlannedPath],
    remote_configs: list[PlannedPath],
    pre_config_commands: tuple[str | ArgumentTemplate, ...],
) -> OpenOcdBasePlan:
    """Put runner setup before board configs, after the opaque configured prefix."""

    argv = executable_argv(executable)
    literal_prefix = len(argv)
    templates: list[tuple[int, ArgumentTemplate]] = []

    def append(value: str, template: ArgumentTemplate | None = None) -> None:
        if template is not None:
            templates.append((len(argv), template))
        argv.append(value)

    for path in remote_search:
        argv.append("-s")
        append(path.remote, path.template)
    for command in pre_config_commands:
        argv.append("-c")
        if isinstance(command, ArgumentTemplate):
            append(command.preview(), command)
        else:
            append(command)
    if serial:
        # Board configurations may consume this variable while they load.
        argv.extend(("-c", "set _ZEPHYR_BOARD_SERIAL " + serial))
    for path in remote_configs:
        argv.append("-f")
        append(path.remote, path.template)
    return OpenOcdBasePlan(tuple(argv), literal_prefix, tuple(templates))


def plan_openocd_base(
    executable: str | tuple[str, ...],
    serial: str | None,
    search_paths: tuple[str, ...],
    config_files: tuple[str, ...],
    planner: PathPlanner,
    pre_config_commands: tuple[str | ArgumentTemplate, ...],
) -> OpenOcdBasePlan:
    """Plan executable, support paths, runner setup, serial, and board configs."""

    executable_parts = tuple(executable_argv(executable))
    remote_search, remote_configs = plan_support_paths(search_paths, config_files, planner)
    return base_argv(executable_parts, serial, remote_search, remote_configs, pre_config_commands)
