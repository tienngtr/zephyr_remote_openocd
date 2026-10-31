# SPDX-License-Identifier: Apache-2.0

"""Shared remote runner operations; upstream OpenOCD adaptation stays separate."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Protocol

from runners.core import FileType, RunnerConfig  # pylint: disable=no-name-in-module

from zephyr_remote_openocd.config import ConfigError, ResolvedRemote, load_config, resolve_remote
from zephyr_remote_openocd.controller._processes import run_finite
from zephyr_remote_openocd.controller._rtt import run_rtt_client
from zephyr_remote_openocd.controller.api import (
    ForwardAdvisory,
    SessionController,
    SessionPlan,
    SshCommand,
    query_remote_openocd_version,
)
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.paths import PathPlanner, PathPlanningError
from zephyr_remote_openocd.openocd.debug import (
    DebugInputs,
    DebugPlan,
    DebugPlanError,
    OpenOcdVersion,
    build_debug_plan,
    parse_openocd_version,
)
from zephyr_remote_openocd.openocd.flash import (
    FlashInputs,
    FlashMode,
    FlashOperation,
    FlashPlanError,
    build_flash_plan,
    validate_flash_inputs,
)


class RemoteRunner(Protocol):
    """Public runner state and execution services consumed by remote operations."""

    remote_config: RunnerConfig
    parsed_args: argparse.Namespace
    logger: logging.Logger
    image_type: FileType | None
    openocd_cmd: list[str]
    openocd_config: list[str] | None
    pre_init: list[str]
    reset_halt_cmd: str
    pre_load: list[str]
    erase_cmd: list[str] | None
    load_cmd: str | None
    verify_cmd: str | None
    post_verify: list[str]
    do_verify: bool
    do_verify_only: bool
    do_erase: bool
    flash_address: str | None
    tcl_port: int | str
    telnet_port: int | str
    gdb_port: int | str
    gdb_client_port: int | str
    gdb_init: list[str] | None
    target_handle: str
    rtt_port: int | str

    @property
    def thread_info_enabled(self) -> bool: ...

    def name(self) -> str: ...

    def require(self, program: str) -> str: ...

    def run_client(self, client: list[str]) -> None: ...

    def get_rtt_address(self) -> int | None: ...


def run_operation(runner: RemoteRunner, command: str) -> None:
    """Select configuration and enter recording or the owned live operation."""
    recording = os.environ.get("ZRO_RECORD") == "1"
    selected = _select_remote(runner, recording)
    if recording:
        _record_runner(runner, command, selected)
        return
    run_live_operation(runner, command, selected)


def run_live_operation(runner: RemoteRunner, command: str, selected: ResolvedRemote) -> None:
    """Own runner Ctrl-C for live preparation and the session, then project it."""
    try:
        with operation_scope():
            try:
                selected, request, plan = build_operation(runner, command, selected)
            except (
                ConfigError,
                FlashPlanError,
                DebugPlanError,
                PathPlanningError,
            ) as error:
                raise RuntimeError(str(error)) from error
            execute_operation(runner, command, request, plan)
    except OperationCancelled as cancelled:
        # Capture is restored and settlement attempted; cancellation is primary.
        raise _interrupted(runner, cancelled) from None


def _interrupted(runner: RemoteRunner, cancelled: OperationCancelled) -> KeyboardInterrupt:
    """Project primary cancellation to stock KeyboardInterrupt behavior.

    West reports KeyboardInterrupt only through its exit status, so retained
    secondary diagnostics, such as cleanup failures, are logged first.
    """
    for note in getattr(cancelled, "__notes__", ()):
        runner.logger.error("%s", note)
    return KeyboardInterrupt()


def _select_remote(runner: RemoteRunner, recording: bool) -> ResolvedRemote:
    try:
        document = load_config()
        return resolve_remote(
            document,
            getattr(runner.parsed_args, "remote", None),
            require_openocd=not recording,
        )
    except ConfigError as error:
        raise RuntimeError(str(error)) from error


def build_operation(
    runner: RemoteRunner, command: str, selected: ResolvedRemote
) -> tuple[ResolvedRemote, SessionPlan, DebugPlan | None]:
    if command == "flash":
        # Locally unperformable requests fail before remote-home resolution.
        validate_flash_inputs(_flash_inputs(runner, selected))
        selected = prepare_remote_paths(selected)
        return selected, _flash_request(runner, selected), None
    selected = prepare_remote_paths(selected)
    if command not in {"debug", "attach", "debugserver", "rtt"}:
        raise RuntimeError(f"remote_openocd {command} is not implemented")
    version = None
    if runner.thread_info_enabled:
        version = parse_openocd_version(
            query_remote_openocd_version(
                SshCommand(selected.ssh_command),
                selected.remote_host,
                _remote_openocd(selected),
            )
        )
    plan = _debug_plan(runner, command, selected, version)
    return selected, debug_request(runner, selected, plan), plan


def execute_operation(
    runner: RemoteRunner, command: str, request: SessionPlan, plan: DebugPlan | None
) -> None:
    session = SessionController.prepare(
        request,
        advisory_handler=lambda advisory: _report_forward_advisory(runner, advisory),
        cancellation_handler=lambda: runner.logger.info(
            "Interrupted; cleaning up the remote OpenOCD session"
        ),
    )
    try:
        session.acquire(
            output_handler=write_output,
            process_start_handler=lambda argv: runner.logger.debug(
                "Remote OpenOCD: %s", shlex.join(argv)
            ),
        )
        if session.descriptor is not None:
            descriptor = session.descriptor
            runner.logger.info(
                "Remote session %s OpenOCD workspace=%s bindto=%s",
                descriptor.session_id,
                descriptor.remote_workspace,
                descriptor.remote_address,
            )
        execute_started_operation(runner, command, plan, session)
    except BaseException as error:
        session.record_operation_failure(error)
    finally:
        finalize_operation(session)


def finalize_operation(session: SessionController) -> None:
    """Publish the result selected by the session's single failure authority."""
    session.finish()


def execute_started_operation(
    runner: RemoteRunner,
    command: str,
    plan: DebugPlan | None,
    session: SessionController,
) -> int | None:
    if command == "rtt":
        return execute_rtt(runner, plan, session)
    _report_rtt_forward(runner, plan, session)
    if command in {"debug", "attach"}:
        return execute_gdb_client(runner, plan, session)
    return execute_server(runner, command, plan, session)


def execute_gdb_client(
    runner: RemoteRunner, plan: DebugPlan | None, session: SessionController
) -> int | None:
    assert plan is not None and plan.gdb_argv is not None
    gdb_argv = plan.gdb_argv
    runner.require(gdb_argv[0])
    returncode = session.poll_operation()
    if returncode is not None:
        raise RuntimeError(f"remote OpenOCD exited before GDB startup with status {returncode}")
    # The configured client, including any wrapper, owns terminal Ctrl-C.
    session.run_interactive_client(lambda: runner.run_client(list(gdb_argv)))
    return session.poll_operation()


def execute_rtt(
    runner: RemoteRunner, plan: DebugPlan | None, session: SessionController
) -> int | None:
    assert plan is not None and plan.gdb_argv is not None
    assert plan.rtt_forward is not None
    gdb_argv = plan.gdb_argv
    rtt_forward = plan.rtt_forward
    runner.require(gdb_argv[0])
    returncode = session.poll_operation()
    if returncode is not None:
        raise RuntimeError(f"remote OpenOCD exited before GDB startup with status {returncode}")
    session.run_local_client(lambda: _run_batch_client(runner, list(gdb_argv)))
    session.make_forwards_optional(
        tuple(forward for forward in plan.forwards if forward.name == "gdb")
    )
    session.forward((rtt_forward,), required=True)
    _report_rtt_forward(runner, plan, session)

    returncode = session.poll_operation()
    if returncode is not None:
        raise RuntimeError(f"remote OpenOCD exited before RTT startup with status {returncode}")
    return session.run_local_client(lambda: run_rtt_client(rtt_forward.local_port, session))


def _run_batch_client(runner: RemoteRunner, argv: list[str]) -> None:
    """Run batch GDB setup as cancellable work with check_call() result semantics."""
    runner.logger.debug(shlex.join(argv))
    result = run_finite(argv, capture_output=False)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, argv)


def execute_server(
    runner: RemoteRunner,
    command: str,
    plan: DebugPlan | None,
    session: SessionController,
) -> int:
    if command == "debugserver":
        assert plan is not None
        gdb = next(item for item in plan.forwards if item.name == "gdb")
        runner.logger.info(
            "Remote OpenOCD GDB forwarding established at 127.0.0.1:%s",
            gdb.local_port,
        )
    return session.wait_for_openocd_exit()


def _report_rtt_forward(
    runner: RemoteRunner, plan: DebugPlan | None, session: SessionController
) -> None:
    if plan is not None and plan.rtt_forward in session.forwarded_specs:
        runner.logger.info(
            "Remote OpenOCD RTT forwarding established at 127.0.0.1:%s",
            plan.rtt_forward.local_port,
        )


def _report_forward_advisory(runner: RemoteRunner, advisory: ForwardAdvisory) -> None:
    runner.logger.warning(
        "Best-effort %s forwarding %s failure on 127.0.0.1:%s: %s",
        advisory.forward.name,
        advisory.phase,
        advisory.forward.local_port,
        advisory.failure,
    )
    for note in getattr(advisory.failure, "__notes__", ()):
        runner.logger.warning(
            "Best-effort %s forwarding diagnostic: %s", advisory.forward.name, note
        )


def _record_runner(runner: RemoteRunner, command: str, selected: ResolvedRemote) -> None:
    request, local_gdb, thread_info, rtt = record_operation(runner, command, selected)
    payload = {
        "recording": True,
        "runner": runner.name(),
        "command": command,
        "runner_args": _recorded_runner_args(runner),
        "runner_config": _recorded_runner_config(runner),
        "selected_config": selected.printable(),
        "remote_session_request": _request_record(request) if request is not None else None,
        "local_gdb_argv": local_gdb,
        "thread_info": thread_info,
        "rtt": rtt,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _recorded_runner_args(runner: RemoteRunner) -> dict[str, object]:
    return {
        key: value
        for key, value in vars(runner.parsed_args).items()
        if value not in (None, False, "", [])
    }


def _recorded_runner_config(runner: RemoteRunner) -> dict[str, object]:
    return {
        key: _json_value(getattr(runner.remote_config, key, None))
        for key in (
            "board_dir",
            "elf_file",
            "hex_file",
            "bin_file",
            "file",
            "file_type",
            "gdb",
            "openocd",
            "openocd_search",
        )
    }


def record_operation(
    runner: RemoteRunner, command: str, selected: ResolvedRemote
) -> tuple[
    SessionPlan | None, list[str] | None, dict[str, object] | None, dict[str, object] | None
]:
    if not selected.remote_host or not selected.remote_openocd:
        return None, None, None, None
    if command == "flash":
        return _flash_request(runner, selected), None, None, None
    if command not in {"debug", "attach", "debugserver", "rtt"}:
        return None, None, None, None
    requested = runner.thread_info_enabled
    supplied = os.environ.get("ZRO_RECORD_OPENOCD_VERSION")
    if requested and supplied is None:
        raise RuntimeError(
            "ZRO_RECORD_OPENOCD_VERSION is required to record a thread-info-enabled build"
        )
    version = parse_openocd_version(supplied) if requested and supplied is not None else None
    plan = _debug_plan(runner, command, selected, version)
    thread_info: dict[str, object] = {
        "requested": requested,
        "version": supplied if requested else None,
        "version_source": "injected" if requested else None,
        "rtos_awareness": plan.rtos_awareness,
    }
    rtt = _recorded_rtt(runner, command, plan)
    local_gdb = list(plan.gdb_argv) if plan.gdb_argv is not None else None
    return debug_request(runner, selected, plan), local_gdb, thread_info, rtt


def _recorded_rtt(runner: RemoteRunner, command: str, plan: DebugPlan) -> dict[str, object]:
    forward = plan.rtt_forward
    return {
        "enabled": forward is not None,
        "address": runner.get_rtt_address() if forward is not None else None,
        "port": forward.local_port if forward is not None else None,
        "setup": plan.rtt_setup,
        "service_phase": ("deferred" if command == "rtt" else "initial")
        if forward is not None
        else None,
        "launches_local_client": plan.launches_rtt_client,
    }


def _flash_inputs(runner: RemoteRunner, selected: ResolvedRemote) -> FlashInputs:
    return FlashInputs(
        executable=_remote_openocd(selected),
        image_type=_file_type(runner.image_type),
        file=runner.remote_config.file,
        elf_file=runner.remote_config.elf_file,
        hex_file=runner.remote_config.hex_file,
        bin_file=runner.remote_config.bin_file,
        search_paths=_search_paths(runner),
        config_files=tuple(runner.openocd_config or ()),
        pre_init=tuple(runner.pre_init),
        reset_halt=runner.reset_halt_cmd,
        pre_load=tuple(runner.pre_load),
        erase_commands=tuple(runner.erase_cmd or ()),
        load_command=runner.load_cmd,
        verify_command=runner.verify_cmd,
        post_verify=tuple(runner.post_verify),
        operation=_flash_operation(runner),
        serial=runner.parsed_args.serial or None,
        flash_address=runner.flash_address,
        no_init=runner.parsed_args.no_init,
        no_targets=runner.parsed_args.no_targets,
    )


def _flash_operation(runner: RemoteRunner) -> FlashOperation:
    """Normalize Zephyr's flash flags; --verify-only takes precedence over --verify."""
    if runner.do_verify_only:
        mode = FlashMode.VERIFY_ONLY
    elif runner.do_verify:
        mode = FlashMode.PROGRAM_AND_VERIFY
    else:
        mode = FlashMode.PROGRAM
    return FlashOperation(mode, erase=runner.do_erase)


def _flash_request(runner: RemoteRunner, selected: ResolvedRemote) -> SessionPlan:
    environment = _forwarded_environment(runner, selected)
    planner = PathPlanner(selected.path_mappings)
    plan = build_flash_plan(_flash_inputs(runner, selected), planner, environment)
    return SessionPlan(
        selected.remote_host,
        SshCommand(selected.ssh_command),
        plan.process,
        plan.staged_files,
        (),
    )


def _remote_openocd(selected: ResolvedRemote) -> tuple[str, ...]:
    return selected.openocd_command


def _forwarded_environment(
    runner: RemoteRunner, selected: ResolvedRemote
) -> tuple[tuple[str, str], ...]:
    environment = []
    for name in selected.forward_env:
        value = os.environ.get(name)
        if value is None:
            runner.logger.warning(
                "allow-listed environment variable %s is absent; omitting it", name
            )
        else:
            environment.append((name, value))
    return tuple(environment)


def prepare_remote_paths(selected: ResolvedRemote) -> ResolvedRemote:
    """Resolve remote ``~`` paths once before any OpenOCD planning."""
    if not _needs_remote_home(selected.openocd_command[0]) and not any(
        _needs_remote_home(str(mapping.remote)) for mapping in selected.path_mappings
    ):
        return selected
    ssh = SshCommand(selected.ssh_command)
    code = "import json,pathlib; print(json.dumps(str(pathlib.Path.home())), flush=True)"
    try:
        result = ssh.run(selected.ssh_host, "python3 -c " + shlex.quote(code), timeout=30)
    except subprocess.TimeoutExpired as error:
        raise ConfigError(
            f"cannot resolve remote home for {selected.name}: "
            f"timed out after {error.timeout:g} seconds"
        ) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise ConfigError(f"cannot resolve remote home for {selected.name}: {detail}")
    try:
        home = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(
            f"remote home query returned an invalid path for {selected.name}"
        ) from error
    if not isinstance(home, str) or not home.startswith("/") or "\0" in home:
        raise ConfigError(f"remote home query returned an invalid path for {selected.name}")

    def expand(value: str) -> str:
        if value == "~":
            return home
        return home + value[1:] if value.startswith("~/") else value

    mappings = tuple(
        replace(item, remote=PurePosixPath(expand(str(item.remote))))
        for item in selected.path_mappings
    )
    return replace(
        selected,
        openocd_command=(expand(selected.openocd_command[0]), *selected.openocd_command[1:]),
        path_mappings=mappings,
    )


def _needs_remote_home(value: str) -> bool:
    return value == "~" or value.startswith("~/")


def _search_paths(runner: RemoteRunner) -> tuple[str, ...]:
    return tuple(
        runner.openocd_cmd[index + 1]
        for index, argument in enumerate(runner.openocd_cmd[:-1])
        if argument == "-s"
    )


def _debug_plan(
    runner: RemoteRunner,
    command: str,
    selected: ResolvedRemote,
    version: OpenOcdVersion | None,
) -> DebugPlan:
    planner = PathPlanner(selected.path_mappings)
    sentinel_suffix = secrets.token_hex(16)
    rtt_requested = command == "rtt" or runner.parsed_args.rtt_server
    return build_debug_plan(
        DebugInputs(
            command=command,
            executable=_remote_openocd(selected),
            gdb=runner.remote_config.gdb,
            elf_file=runner.remote_config.elf_file,
            search_paths=_search_paths(runner),
            config_files=tuple(runner.openocd_config or ()),
            pre_init=tuple(runner.pre_init),
            reset_halt=runner.reset_halt_cmd,
            serial=runner.parsed_args.serial or None,
            no_halt=runner.parsed_args.no_halt,
            no_init=runner.parsed_args.no_init,
            no_targets=runner.parsed_args.no_targets,
            tcl_port=runner.tcl_port,
            telnet_port=runner.telnet_port,
            gdb_port=runner.gdb_port,
            gdb_client_port=runner.gdb_client_port,
            gdb_init=tuple(runner.gdb_init or ()),
            tui=bool(runner.parsed_args.tui),
            load=bool(runner.parsed_args.load),
            target_handle=runner.target_handle,
            thread_info_requested=runner.thread_info_enabled,
            openocd_version=version,
            openocd_init_sentinel="ZRO_OPENOCD_INIT_" + sentinel_suffix,
            startup_complete_sentinel="ZRO_STARTUP_COMPLETE_" + sentinel_suffix,
            rtt_address=runner.get_rtt_address() if rtt_requested else None,
            rtt_port=runner.rtt_port,
            rtt_server=bool(runner.parsed_args.rtt_server),
        ),
        planner,
        _forwarded_environment(runner, selected),
    )


def debug_request(runner: RemoteRunner, selected: ResolvedRemote, plan: DebugPlan) -> SessionPlan:
    return SessionPlan(
        selected.remote_host,
        SshCommand(selected.ssh_command),
        plan.process,
        plan.staged_files,
        plan.forwards,
        plan.auxiliary_forwards,
        plan.reserved_listeners,
    )


def _json_value(value: object) -> object:
    if hasattr(value, "value"):
        return value.value
    return value


def _request_record(request: SessionPlan) -> dict[str, object]:
    return {
        "host": request.host,
        "ssh_command": list(request.ssh_command.argv_prefix),
        "staged_files": [
            {"source": str(item.source), "destination": str(item.destination)}
            for item in request.staged_files
        ],
        "services": [
            {
                "name": item.name,
                "local_port": item.local_port,
                "remote_port": item.remote_port,
                "criticality": "auxiliary" if item in request.auxiliary_forwards else "required",
            }
            for item in request.forwards
        ],
        "reserved_services": [
            {
                "name": item.name,
                "local_port": item.remote_port,
                "remote_port": item.remote_port,
            }
            for item in request.reserved_listeners
        ],
        "process": {
            # The current session model has one generic remote process path;
            # retain the historical recording label at this output boundary.
            "kind": "openocd",
            "argv": list(request.process.argv),
            "environment": [name for name, _ in request.process.environment],
            "required_paths": [
                {
                    "path": item.path
                    if item.template is None
                    else {"parts": item.template.wire_parts()},
                    "kind": item.kind,
                }
                for item in request.process.required_paths
            ],
            "required_output_sentinels": list(request.process.required_output_sentinels),
            "readiness_timeout": request.process.readiness_timeout,
            "literal_prefix": request.process.literal_prefix,
            "argv_templates": [
                {"index": index, "parts": template.wire_parts()}
                for index, template in request.process.argv_templates
            ],
        },
    }


def _file_type(value: object) -> str | None:
    if value == FileType.ELF:
        return "elf"
    if value == FileType.HEX:
        return "hex"
    if value == FileType.BIN:
        return "bin"
    if value == FileType.OTHER:
        return "other"
    return None


def write_output(stream: str, payload: str, line_end: bool) -> None:
    destination = sys.stderr if stream == "stderr" else sys.stdout
    destination.write(payload)
    if line_end:
        destination.write("\n")
    destination.flush()
