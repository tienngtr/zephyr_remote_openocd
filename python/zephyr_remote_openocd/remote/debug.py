# SPDX-License-Identifier: Apache-2.0

"""Pure construction of persistent remote OpenOCD debug plans."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .model import RemoteProcess, Service
from .paths import ADDRESS_TOKEN, PathPlanner


class DebugPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenOcdVersion:
    major: int
    minor: int
    revision: int
    text: str

    @property
    def zephyr_tuple(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.revision


def parse_openocd_version(output: str) -> OpenOcdVersion:
    match = re.search(r"Open On-Chip Debugger.*?v?(\d+)\.(\d+)\.(\d+)(\+dev)?", output)
    if match is None:
        raise DebugPlanError("cannot parse remote OpenOCD version")
    major, minor, revision = map(int, match.group(1, 2, 3))
    if match.group(4):
        revision += 1
    return OpenOcdVersion(major, minor, revision, match.group(0))


def thread_info_enabled(requested: bool, version: OpenOcdVersion | None) -> bool:
    if not requested:
        return False
    if version is None:
        raise DebugPlanError("OpenOCD version is required when Zephyr thread info is enabled")
    return version.zephyr_tuple > (0, 11, 0)


@dataclass(frozen=True)
class DebugInputs:
    command: str
    executable: str
    gdb: str | None
    elf_file: str | None
    search_paths: tuple[str, ...]
    config_files: tuple[str, ...]
    pre_init: tuple[str, ...] = ()
    reset_halt: str = "reset init"
    serial: str | None = None
    no_halt: bool = False
    no_init: bool = False
    no_targets: bool = False
    tcl_port: int | str = 6333
    telnet_port: int | str = 4444
    gdb_port: int | str = 3333
    gdb_client_port: int | str = 3333
    gdb_init: tuple[str, ...] = ()
    tui: bool = False
    load: bool = True
    target_handle: str = "_TARGETNAME"
    thread_info_requested: bool = False
    openocd_version: OpenOcdVersion | None = None
    readiness_marker: str = ""


@dataclass(frozen=True)
class DebugPlan:
    process: RemoteProcess
    staged_files: tuple
    services: tuple[Service, ...]
    gdb_argv: tuple[str, ...] | None
    thread_info_requested: bool
    openocd_version: OpenOcdVersion | None
    rtos_awareness: bool


def _port(value: int | str, name: str, *, required: bool = False) -> int | None:
    if isinstance(value, str) and value.lower() == "disabled":
        if required:
            raise DebugPlanError(f"{name} must be enabled for this command")
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise DebugPlanError(f"{name} must be 'disabled' or a port in 1..65535") from error
    if not 1 <= port <= 65535:
        raise DebugPlanError(f"{name} must be 'disabled' or a port in 1..65535")
    return port


def _commands(commands: tuple[str, ...]) -> list[str]:
    return [item for command in commands for item in ("-c", command)]


def build_debug_plan(
    inputs: DebugInputs,
    planner: PathPlanner,
    environment: tuple[tuple[str, str], ...] = (),
) -> DebugPlan:
    if inputs.command not in {"debug", "attach", "debugserver"}:
        raise DebugPlanError(f"unsupported persistent debug command: {inputs.command}")
    if not inputs.readiness_marker or any(ch.isspace() for ch in inputs.readiness_marker):
        raise DebugPlanError("readiness marker must be a non-empty token")

    remote_gdb = _port(inputs.gdb_port, "gdb_port", required=True)
    local_gdb = _port(inputs.gdb_client_port, "gdb_client_port", required=True)
    remote_tcl = _port(inputs.tcl_port, "tcl_port")
    remote_telnet = _port(inputs.telnet_port, "telnet_port")
    assert remote_gdb is not None and local_gdb is not None
    services = [Service("gdb", local_gdb, remote_gdb)]
    if remote_tcl is not None:
        services.append(Service("tcl", remote_tcl, remote_tcl))
    if remote_telnet is not None:
        services.append(Service("telnet", remote_telnet, remote_telnet))

    indexed_search = list(enumerate(inputs.search_paths))
    planned_search = {}
    for index, path in sorted(indexed_search, key=lambda item: len(Path(item[1]).resolve().parts)):
        planned_search[index] = planner.plan_directory(Path(path), f"search-{index}").remote
    remote_search = [planned_search[index] for index, _ in indexed_search]
    remote_configs = [
        planner.plan_file(Path(path), f"config-{index}").remote
        for index, path in enumerate(inputs.config_files)
    ]

    rtos = thread_info_enabled(inputs.thread_info_requested, inputs.openocd_version)
    argv = [inputs.executable]
    for path in remote_search:
        argv.extend(("-s", path))
    for path in remote_configs:
        argv.extend(("-f", path))
    if inputs.serial:
        argv.extend(("-c", "set _ZEPHYR_BOARD_SERIAL " + inputs.serial))
    argv.extend(("-c", f"bindto {ADDRESS_TOKEN}"))
    for name, port in (
        ("tcl_port", remote_tcl),
        ("telnet_port", remote_telnet),
        ("gdb_port", remote_gdb),
    ):
        argv.extend(("-c", f"{name} {port if port is not None else 'disabled'}"))
    argv.extend(_commands(inputs.pre_init))
    if rtos:
        argv.extend(("-c", f"${inputs.target_handle} configure -rtos Zephyr"))
    if not inputs.no_init:
        argv.extend(("-c", "init"))
    if not inputs.no_targets:
        argv.extend(("-c", "targets"))
    if inputs.command == "debugserver":
        argv.extend(("-c", inputs.reset_halt))
    elif not inputs.no_halt:
        argv.extend(("-c", "halt"))
    argv.extend(("-c", f"echo {inputs.readiness_marker}"))

    gdb_argv = None
    if inputs.command != "debugserver":
        if not inputs.gdb:
            raise DebugPlanError(f"cannot {inputs.command}; no GDB executable specified")
        if not inputs.elf_file:
            raise DebugPlanError(f"cannot {inputs.command}; no ELF file specified")
        client = [inputs.gdb]
        if inputs.tui:
            client.append("-tui")
        client.extend(("-ex", f"target extended-remote 127.0.0.1:{local_gdb}", inputs.elf_file))
        if inputs.command == "debug" and inputs.load:
            client.extend(("-ex", "load"))
        for command in inputs.gdb_init:
            client.extend(("-ex", command))
        gdb_argv = tuple(client)

    process = RemoteProcess(
        "openocd",
        tuple(argv),
        environment,
        tuple(planner.remote_checks),
        inputs.readiness_marker,
        30.0,
    )
    return DebugPlan(
        process,
        tuple(planner.staged_files),
        tuple(services),
        gdb_argv,
        inputs.thread_info_requested,
        inputs.openocd_version,
        rtos,
    )
