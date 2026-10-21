# SPDX-License-Identifier: Apache-2.0

"""Zephyr 4.4 runner integration for recording and remote OpenOCD operations."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Self

from runners.core import FileType, RunnerConfig  # pylint: disable=no-name-in-module
from runners.openocd import OpenOcdBinaryRunner  # pylint: disable=no-name-in-module

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
    FlashPlanError,
    build_flash_plan,
    validate_flash_inputs,
)


class RemoteOpenOcdBinaryRunner(OpenOcdBinaryRunner):
    """OpenOCD-compatible runner with a version-isolated flash integration."""

    def __init__(self, cfg: RunnerConfig, parsed_args: argparse.Namespace) -> None:
        # Reuse OpenOCD's public initializer and state construction, but never
        # call its local process-launching implementation.
        image_type = cfg.file_type
        if parsed_args.use_image_type and (image_type == FileType.OTHER or image_type is None):
            image_type = {"elf": FileType.ELF, "hex": FileType.HEX, "bin": FileType.BIN}[
                parsed_args.use_image_type
            ]
        # Upstream inspects each existing config's parent during initialization.
        # A bare cwd filename has an empty dirname there; normalize existing
        # local selections while leaving search-relative references intact.
        config = (
            [
                str(Path(item).absolute()) if Path(item).exists() else item
                for item in parsed_args.config
            ]
            if parsed_args.config
            else parsed_args.config
        )
        super().__init__(
            cfg,
            pre_init=parsed_args.cmd_pre_init,
            reset_halt_cmd=parsed_args.cmd_reset_halt,
            pre_load=parsed_args.cmd_pre_load,
            erase_cmd=parsed_args.cmd_erase,
            load_cmd=parsed_args.cmd_load,
            verify_cmd=parsed_args.cmd_verify,
            post_verify=parsed_args.cmd_post_verify,
            do_verify=parsed_args.verify,
            do_verify_only=parsed_args.verify_only,
            do_erase=parsed_args.erase,
            tui=parsed_args.tui,
            config=config,
            serial=parsed_args.serial,
            image_type=image_type,
            flash_address=parsed_args.flash_address,
            no_halt=parsed_args.no_halt,
            no_init=parsed_args.no_init,
            no_targets=parsed_args.no_targets,
            tcl_port=parsed_args.tcl_port,
            telnet_port=parsed_args.telnet_port,
            gdb_port=parsed_args.gdb_port,
            gdb_client_port=parsed_args.gdb_client_port,
            gdb_init=parsed_args.gdb_init,
            load=parsed_args.load,
            target_handle=parsed_args.target_handle,
            rtt_port=parsed_args.rtt_port,
            rtt_server=parsed_args.rtt_server,
        )
        self.remote_config = cfg
        self.parsed_args = parsed_args

    @classmethod
    def name(cls) -> str:
        return "remote_openocd"

    @classmethod
    def do_create(cls, cfg: RunnerConfig, args: argparse.Namespace) -> Self:
        return cls(cfg, args)

    @classmethod
    def do_add_parser(cls, parser: argparse.ArgumentParser) -> None:
        super().do_add_parser(parser)
        parser.add_argument("--remote", help="select a configured remote by name")

    def do_run(self, command: str, **kwargs: object) -> None:
        recording = os.environ.get("ZRO_RECORD") == "1"
        selected = _select_remote(self, recording)
        if recording:
            _record_runner(self, command, selected)
            return
        run_live_operation(self, command, selected)


def run_live_operation(
    runner: RemoteOpenOcdBinaryRunner, command: str, selected: ResolvedRemote
) -> None:
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


def _interrupted(
    runner: RemoteOpenOcdBinaryRunner, cancelled: OperationCancelled
) -> KeyboardInterrupt:
    """Project primary cancellation to stock KeyboardInterrupt behavior.

    West reports KeyboardInterrupt only through its exit status, so retained
    secondary diagnostics, such as cleanup failures, are logged first.
    """
    for note in getattr(cancelled, "__notes__", ()):
        runner.logger.error("%s", note)
    return KeyboardInterrupt()


def _select_remote(runner: RemoteOpenOcdBinaryRunner, recording: bool) -> ResolvedRemote:
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
    runner: RemoteOpenOcdBinaryRunner, command: str, selected: ResolvedRemote
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
    runner: RemoteOpenOcdBinaryRunner, command: str, request: SessionPlan, plan: DebugPlan | None
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
    runner: RemoteOpenOcdBinaryRunner,
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
    runner: RemoteOpenOcdBinaryRunner, plan: DebugPlan | None, session: SessionController
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
    runner: RemoteOpenOcdBinaryRunner, plan: DebugPlan | None, session: SessionController
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


def _run_batch_client(runner: RemoteOpenOcdBinaryRunner, argv: list[str]) -> None:
    """Run batch GDB setup as cancellable work with check_call() result semantics."""
    runner.logger.debug(shlex.join(argv))
    result = run_finite(argv, capture_output=False)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, argv)


def execute_server(
    runner: RemoteOpenOcdBinaryRunner,
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
    runner: RemoteOpenOcdBinaryRunner, plan: DebugPlan | None, session: SessionController
) -> None:
    if plan is not None and plan.rtt_forward in session.forwarded_specs:
        runner.logger.info(
            "Remote OpenOCD RTT forwarding established at 127.0.0.1:%s",
            plan.rtt_forward.local_port,
        )


def _report_forward_advisory(runner: RemoteOpenOcdBinaryRunner, advisory: ForwardAdvisory) -> None:
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


def _record_runner(
    runner: RemoteOpenOcdBinaryRunner, command: str, selected: ResolvedRemote
) -> None:
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


def _recorded_runner_args(runner: RemoteOpenOcdBinaryRunner) -> dict[str, object]:
    return {
        key: value
        for key, value in vars(runner.parsed_args).items()
        if value not in (None, False, "", [])
    }


def _recorded_runner_config(runner: RemoteOpenOcdBinaryRunner) -> dict[str, object]:
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
    runner: RemoteOpenOcdBinaryRunner, command: str, selected: ResolvedRemote
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


def _recorded_rtt(
    runner: RemoteOpenOcdBinaryRunner, command: str, plan: DebugPlan
) -> dict[str, object]:
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


def _flash_inputs(runner: RemoteOpenOcdBinaryRunner, selected: ResolvedRemote) -> FlashInputs:
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
        verify=runner.do_verify,
        verify_only=runner.do_verify_only,
        erase=runner.do_erase,
        serial=runner.parsed_args.serial or None,
        flash_address=runner.flash_address,
        no_init=runner.parsed_args.no_init,
        no_targets=runner.parsed_args.no_targets,
    )


def _flash_request(runner: RemoteOpenOcdBinaryRunner, selected: ResolvedRemote) -> SessionPlan:
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
    runner: RemoteOpenOcdBinaryRunner, selected: ResolvedRemote
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


def _search_paths(runner: RemoteOpenOcdBinaryRunner) -> tuple[str, ...]:
    return tuple(
        runner.openocd_cmd[index + 1]
        for index, argument in enumerate(runner.openocd_cmd[:-1])
        if argument == "-s"
    )


def _debug_plan(
    runner: RemoteOpenOcdBinaryRunner,
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


def debug_request(
    runner: RemoteOpenOcdBinaryRunner, selected: ResolvedRemote, plan: DebugPlan
) -> SessionPlan:
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
