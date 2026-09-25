# SPDX-License-Identifier: Apache-2.0

"""Pure construction of persistent remote OpenOCD debug plans."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import RemoteProcess, Service
from .openocd_plan import plan_openocd_base
from .paths import REMOTE_ADDRESS_PLACEHOLDER, PathPlanner

RTT_SEARCH_RANGE_SIZE = 0x10
RTT_CHANNEL = 0
THREAD_INFO_VERSION_THRESHOLD = (0, 11, 0)
OPENOCD_READINESS_TIMEOUT = 30.0


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
    match = re.search(r"Open On-Chip Debugger.*?v?(\d+)\.(\d+)\.(\d+)(?:\+dev)?", output)
    if match is None:
        raise DebugPlanError("cannot parse remote OpenOCD version")
    major, minor, revision = map(int, match.group(1, 2, 3))
    # Zephyr 4.4 compares the numeric version only; ``+dev`` is not a
    # version increment for the thread-info capability decision.
    return OpenOcdVersion(major, minor, revision, match.group(0))


def thread_info_enabled(requested: bool, version: OpenOcdVersion | None) -> bool:
    if not requested:
        return False
    if version is None:
        raise DebugPlanError("OpenOCD version is required when Zephyr thread info is enabled")
    return version.zephyr_tuple > THREAD_INFO_VERSION_THRESHOLD


@dataclass(frozen=True)
class DebugInputs:
    command: str
    executable: str | tuple[str, ...]
    gdb: str | None
    elf_file: str | None
    search_paths: tuple[str, ...]
    config_files: tuple[str, ...]
    openocd_init_sentinel: str
    startup_complete_sentinel: str
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
    rtt_address: int | None = None
    rtt_port: int | str = 5555
    rtt_server: bool = False


@dataclass(frozen=True)
class DebugPlan:
    process: RemoteProcess
    staged_files: tuple
    services: tuple[Service, ...]
    gdb_argv: tuple[str, ...] | None
    thread_info_requested: bool
    openocd_version: OpenOcdVersion | None
    rtos_awareness: bool
    rtt_service: Service | None
    rtt_setup: str | None
    launches_rtt_client: bool


@dataclass(frozen=True)
class DebugServicePlan:
    """Validated OpenOCD and optional RTT service allocation."""

    remote_gdb: int
    local_gdb: int
    remote_tcl: int | None
    remote_telnet: int | None
    services: tuple[Service, ...]
    rtt_service: Service | None


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


def _validate_debug_inputs(inputs: DebugInputs) -> None:
    if inputs.command not in {"debug", "attach", "debugserver", "rtt"}:
        raise DebugPlanError(f"unsupported persistent debug command: {inputs.command}")


def _plan_debug_services(inputs: DebugInputs) -> DebugServicePlan:
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

    rtt_requested = inputs.command == "rtt" or inputs.rtt_server
    rtt_service = None
    if rtt_requested:
        if inputs.rtt_address is None:
            raise DebugPlanError("RTT control block not found")
        rtt_port = _port(inputs.rtt_port, "rtt_port", required=True)
        assert rtt_port is not None
        if any(rtt_port in (service.local_port, service.remote_port) for service in services):
            raise DebugPlanError("rtt_port conflicts with an enabled OpenOCD service port")
        rtt_service = Service("rtt", rtt_port, rtt_port)
        if inputs.command != "rtt":
            services.append(rtt_service)
    return DebugServicePlan(
        remote_gdb,
        local_gdb,
        remote_tcl,
        remote_telnet,
        tuple(services),
        rtt_service,
    )


def _server_commands(
    inputs: DebugInputs, services: DebugServicePlan, rtos: bool
) -> tuple[str, ...]:
    commands: list[str] = _commands(inputs.pre_init)
    if rtos:
        commands.extend(("-c", f"${inputs.target_handle} configure -rtos Zephyr"))
    if not inputs.no_init:
        commands.extend(("-c", "init"))
    if not inputs.no_targets:
        commands.extend(("-c", "targets"))
    if inputs.command == "debugserver":
        commands.extend(("-c", inputs.reset_halt))
    elif not inputs.no_halt:
        commands.extend(("-c", "halt"))
    if inputs.rtt_server and inputs.command != "rtt":
        assert inputs.rtt_address is not None and services.rtt_service is not None
        commands.extend(
            (
                "-c",
                f'rtt setup 0x{inputs.rtt_address:x} 0x{RTT_SEARCH_RANGE_SIZE:x} "SEGGER RTT"',
                "-c",
                "rtt start",
                "-c",
                f"rtt server start {services.rtt_service.remote_port} {RTT_CHANNEL}",
            )
        )
    commands.extend(("-c", f"echo {inputs.startup_complete_sentinel}"))
    return tuple(commands)


def _rtt_client_commands(inputs: DebugInputs, services: DebugServicePlan) -> tuple[str, ...]:
    assert inputs.rtt_address is not None and services.rtt_service is not None
    return (
        "-ex",
        f'monitor rtt setup 0x{inputs.rtt_address:x} 0x{RTT_SEARCH_RANGE_SIZE:x} "SEGGER RTT"',
        "-ex",
        "monitor reset run",
        "-ex",
        "monitor rtt start",
        "-ex",
        f"monitor rtt server start {services.rtt_service.remote_port} {RTT_CHANNEL}",
        "-ex",
        "detach",
        "-ex",
        "quit",
    )


def _client_argv(inputs: DebugInputs, services: DebugServicePlan) -> tuple[str, ...] | None:
    if inputs.command == "debugserver":
        return None
    if not inputs.gdb:
        raise DebugPlanError(f"cannot {inputs.command}; no GDB executable specified")
    if not inputs.elf_file:
        raise DebugPlanError(f"cannot {inputs.command}; no ELF file specified")
    client = [inputs.gdb]
    if inputs.command == "rtt":
        client.append("--batch")
    if inputs.tui:
        client.append("-tui")
    client.extend(
        ("-ex", f"target extended-remote 127.0.0.1:{services.local_gdb}", inputs.elf_file)
    )
    if inputs.command == "debug" and inputs.load:
        client.extend(("-ex", "load"))
    for command in inputs.gdb_init:
        client.extend(("-ex", command))
    if inputs.command == "rtt":
        client.extend(_rtt_client_commands(inputs, services))
    return tuple(client)


def _rtt_setup(inputs: DebugInputs) -> str | None:
    if inputs.command == "rtt":
        return "batch_gdb"
    if inputs.rtt_server:
        return "openocd_startup"
    return None


def build_debug_plan(
    inputs: DebugInputs,
    planner: PathPlanner,
    environment: tuple[tuple[str, str], ...] = (),
) -> DebugPlan:
    _validate_debug_inputs(inputs)
    services = _plan_debug_services(inputs)
    tcl_port = services.remote_tcl if services.remote_tcl is not None else "disabled"
    telnet_port = services.remote_telnet if services.remote_telnet is not None else "disabled"
    pre_config_commands = (
        f"lappend post_init_commands {{echo {inputs.openocd_init_sentinel}}}",
        f"bindto {REMOTE_ADDRESS_PLACEHOLDER}",
        f"tcl_port {tcl_port}",
        f"telnet_port {telnet_port}",
        f"gdb_port {services.remote_gdb}",
    )
    base = plan_openocd_base(
        inputs.executable,
        inputs.serial,
        inputs.search_paths,
        inputs.config_files,
        planner,
        pre_config_commands,
    )
    rtos = thread_info_enabled(inputs.thread_info_requested, inputs.openocd_version)
    argv = base.argv + _server_commands(inputs, services, rtos)
    gdb_argv = _client_argv(inputs, services)

    process = RemoteProcess(
        argv,
        environment,
        tuple(planner.remote_checks),
        (inputs.openocd_init_sentinel, inputs.startup_complete_sentinel),
        readiness_timeout=OPENOCD_READINESS_TIMEOUT,
        literal_prefix=base.literal_prefix,
    )
    return DebugPlan(
        process,
        tuple(planner.staged_files),
        services.services,
        gdb_argv,
        inputs.thread_info_requested,
        inputs.openocd_version,
        rtos,
        services.rtt_service,
        _rtt_setup(inputs),
        inputs.command == "rtt",
    )
