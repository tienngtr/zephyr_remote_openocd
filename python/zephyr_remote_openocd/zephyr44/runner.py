# SPDX-License-Identifier: Apache-2.0

"""Zephyr 4.4 adapter for recording and remote OpenOCD operations."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import sys
from dataclasses import replace
from pathlib import PurePosixPath

from runners.core import FileType  # pylint: disable=no-name-in-module
from runners.openocd import OpenOcdBinaryRunner  # pylint: disable=no-name-in-module

from zephyr_remote_openocd.config import (
    ConfigError,
    load_config,
    require_remote_settings,
    resolve_remote,
)
from zephyr_remote_openocd.remote import RemoteSession, RemoteSessionRequest, SshHelperBackend
from zephyr_remote_openocd.remote.debug import (
    DebugInputs,
    DebugPlanError,
    build_debug_plan,
    parse_openocd_version,
)
from zephyr_remote_openocd.remote.flash import FlashInputs, FlashPlanError, build_flash_plan
from zephyr_remote_openocd.remote.paths import PathPlanner, PathPlanningError
from zephyr_remote_openocd.remote.rtt import RttClientError, run_rtt_client
from zephyr_remote_openocd.remote.ssh import SshCommand


class RemoteOpenOcdBinaryRunner(OpenOcdBinaryRunner):
    """OpenOCD-compatible runner with a version-isolated flash adapter."""

    def __init__(self, cfg, parsed_args):
        # Reuse OpenOCD's public initializer and state construction, but never
        # call its local process-launching implementation.
        image_type = cfg.file_type
        if parsed_args.use_image_type and (image_type == FileType.OTHER or image_type is None):
            image_type = {"elf": FileType.ELF, "hex": FileType.HEX, "bin": FileType.BIN}[
                parsed_args.use_image_type
            ]
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
            config=parsed_args.config,
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
    def name(cls):
        return "remote_openocd"

    @classmethod
    def do_create(cls, cfg, args):
        return cls(cfg, args)

    @classmethod
    def do_add_parser(cls, parser):
        super().do_add_parser(parser)
        parser.add_argument("--remote", help="select a configured remote by name")

    def do_run(self, command, **kwargs):
        recording = os.environ.get("ZRO_RECORD") == "1"
        selected = _select_remote(self, recording)
        if recording:
            _record_runner(self, command, selected)
            return
        try:
            selected, request, plan, backend = _build_operation(self, command, selected)
        except (
            ConfigError,
            FlashPlanError,
            DebugPlanError,
            PathPlanningError,
            RttClientError,
        ) as error:
            raise RuntimeError(str(error)) from error
        _execute_operation(self, command, request, plan, backend)


def _select_remote(runner, recording):
    try:
        document = load_config()
        return resolve_remote(
            document,
            getattr(runner.parsed_args, "remote", None),
            require_openocd=not recording,
        )
    except ConfigError as error:
        raise RuntimeError(str(error)) from error


def _build_operation(runner, command, selected):
    selected = _prepare_remote_paths(selected)
    backend = SshHelperBackend(output_handler=_write_output)
    if command == "flash":
        return selected, _flash_request(runner, selected), None, backend
    if command not in {"debug", "attach", "debugserver", "rtt"}:
        raise RuntimeError(f"remote_openocd {command} is not implemented")
    version = None
    if runner.thread_info_enabled:
        version = parse_openocd_version(
            backend.openocd_version(
                SshCommand(selected.ssh_command),
                selected.remote_host,
                _remote_openocd(selected, command),
            )
        )
    plan = _debug_plan(runner, command, selected, version)
    return selected, _debug_request(runner, selected, plan), plan, backend


def _execute_operation(runner, command, request, plan, backend):
    session = RemoteSession(request, backend)
    descriptor = session.start()
    observed_returncode = None
    try:
        runner.logger.info(
            "Remote OpenOCD session %s workspace=%s bindto=%s",
            descriptor.session_id,
            descriptor.remote_workspace,
            descriptor.remote_address,
        )
        observed_returncode = _execute_started_operation(runner, command, plan, session)
        if observed_returncode:
            raise RuntimeError(f"remote OpenOCD failed with exit status {observed_returncode}")
    except BaseException as error:
        primary_error = error
        if observed_returncode is None and (returncode := session.termination_returncode):
            observed_returncode = returncode
            primary_error = RuntimeError(
                f"remote OpenOCD failed with exit status {observed_returncode}"
            )
            primary_error.add_note(f"session cleanup also failed: {error}")
        try:
            late_returncode = session.close()
        except BaseException as cleanup_error:
            primary_error.add_note(f"session cleanup also failed: {cleanup_error}")
        else:
            if late_returncode and late_returncode != observed_returncode:
                primary_error.add_note(
                    f"remote OpenOCD also exited with status {late_returncode} during cleanup"
                )
        if primary_error is error:
            raise
        raise primary_error from error
    else:
        late_returncode = session.close()
        if late_returncode:
            raise RuntimeError(f"remote OpenOCD failed with exit status {late_returncode}")


def _execute_started_operation(runner, command, plan, session):
    if command == "rtt":
        return _execute_rtt(runner, plan, session)
    _report_rtt_service(runner, plan)
    if command in {"debug", "attach"}:
        return _execute_gdb_client(runner, plan, session)
    return _execute_server(runner, command, plan, session)


def _execute_gdb_client(runner, plan, session):
    assert plan is not None and plan.gdb_argv is not None
    runner.require(plan.gdb_argv[0])
    runner.run_client(list(plan.gdb_argv))
    return session.poll()


def _execute_rtt(runner, plan, session):
    assert plan is not None and plan.gdb_argv is not None
    assert plan.rtt_service is not None
    runner.require(plan.gdb_argv[0])
    runner.run_client(list(plan.gdb_argv))
    session.forward((plan.rtt_service,))
    _report_rtt_service(runner, plan)
    return run_rtt_client(plan.rtt_service.local_port, session.poll)


def _execute_server(runner, command, plan, session):
    if command == "debugserver":
        assert plan is not None
        gdb = next(item for item in plan.services if item.name == "gdb")
        runner.logger.info(
            "Remote OpenOCD GDB server available at 127.0.0.1:%s",
            gdb.local_port,
        )
    return session.wait()


def _report_rtt_service(runner, plan):
    if plan is not None and plan.rtt_service is not None:
        runner.logger.info(
            "Remote OpenOCD RTT server available at 127.0.0.1:%s",
            plan.rtt_service.local_port,
        )


def _record_runner(runner, command, selected):
    request, local_gdb, thread_info, rtt = _record_operation(runner, command, selected)
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


def _recorded_runner_args(runner):
    return {
        key: value
        for key, value in vars(runner.parsed_args).items()
        if value not in (None, False, "", [])
    }


def _recorded_runner_config(runner):
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


def _record_operation(runner, command, selected):
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
    thread_info = {
        "requested": requested,
        "version": supplied if requested else None,
        "version_source": "injected" if requested else None,
        "rtos_awareness": plan.rtos_awareness,
    }
    rtt = _recorded_rtt(runner, command, plan)
    local_gdb = list(plan.gdb_argv) if plan.gdb_argv is not None else None
    return _debug_request(runner, selected, plan), local_gdb, thread_info, rtt


def _recorded_rtt(runner, command, plan):
    service = plan.rtt_service
    return {
        "enabled": service is not None,
        "address": runner.get_rtt_address() if service is not None else None,
        "port": service.local_port if service is not None else None,
        "setup": plan.rtt_setup,
        "service_phase": ("deferred" if command == "rtt" else "initial")
        if service is not None
        else None,
        "launches_local_client": plan.launches_rtt_client,
    }


def _flash_request(runner, selected):
    executable = _remote_openocd(selected, "flash")
    environment = _forwarded_environment(runner, selected)
    search_paths = _search_paths(runner)
    inputs = FlashInputs(
        executable=executable,
        image_type=_file_type(runner.image_type),
        file=runner.remote_config.file,
        elf_file=runner.remote_config.elf_file,
        hex_file=runner.remote_config.hex_file,
        bin_file=runner.remote_config.bin_file,
        search_paths=search_paths,
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
    planner = PathPlanner(selected.path_mappings)
    plan = build_flash_plan(inputs, planner, environment)
    return RemoteSessionRequest(
        selected.remote_host,
        SshCommand(selected.ssh_command),
        plan.process,
        plan.staged_files,
        (),
    )


def _remote_openocd(selected, command):
    require_remote_settings(selected, command)
    return selected.openocd_command


def _forwarded_environment(runner, selected):
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


def _prepare_remote_paths(selected):
    """Resolve remote ``~`` paths once before any OpenOCD planning."""
    if not _needs_remote_home(selected.openocd_command[0]) and not any(
        _needs_remote_home(str(mapping.remote)) for mapping in selected.path_mappings
    ):
        return selected
    ssh = SshCommand(selected.ssh_command)
    code = "import json,pathlib; print(json.dumps(str(pathlib.Path.home())), flush=True)"
    result = ssh.run(selected.ssh_host, "python3 -c " + shlex.quote(code), timeout=30)
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


def _search_paths(runner):
    return tuple(
        runner.openocd_cmd[index + 1]
        for index, argument in enumerate(runner.openocd_cmd[:-1])
        if argument == "-s"
    )


def _debug_plan(runner, command, selected, version):
    planner = PathPlanner(selected.path_mappings)
    if command == "attach" and runner.parsed_args.rtt_server:
        raise DebugPlanError("--rtt-server is not supported with attach")
    rtt_requested = command == "rtt" or (
        command in {"debug", "debugserver"} and runner.parsed_args.rtt_server
    )
    return build_debug_plan(
        DebugInputs(
            command=command,
            executable=_remote_openocd(selected, command),
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
            readiness_marker="ZRO_READY_" + secrets.token_hex(16),
            rtt_address=runner.get_rtt_address() if rtt_requested else None,
            rtt_port=runner.rtt_port,
            rtt_server=bool(runner.parsed_args.rtt_server),
        ),
        planner,
        _forwarded_environment(runner, selected),
    )


def _debug_request(runner, selected, plan):
    return RemoteSessionRequest(
        selected.remote_host,
        SshCommand(selected.ssh_command),
        plan.process,
        plan.staged_files,
        plan.services,
    )


def _json_value(value):
    if hasattr(value, "value"):
        return value.value
    return value


def _request_record(request):
    result = {
        "host": request.host,
        "ssh_command": list(request.ssh_command.argv_prefix),
        "staged_files": [
            {"source": str(item.source), "destination": str(item.destination)}
            for item in request.staged_files
        ],
        "services": [
            {"name": item.name, "local_port": item.local_port, "remote_port": item.remote_port}
            for item in request.services
        ],
    }
    if request.process is not None:
        result["process"] = {
            # The current session model has one generic remote process path;
            # retain the historical recording label at this output boundary.
            "kind": "openocd",
            "argv": list(request.process.argv),
            "environment": [name for name, _ in request.process.environment],
            "required_paths": [
                {"path": item.path, "kind": item.kind} for item in request.process.required_paths
            ],
            "readiness_marker": request.process.readiness_marker,
            "readiness_timeout": request.process.readiness_timeout,
            "literal_prefix": request.process.literal_prefix,
        }
    return result


def _file_type(value):
    if value == FileType.ELF:
        return "elf"
    if value == FileType.HEX:
        return "hex"
    if value == FileType.BIN:
        return "bin"
    if value == FileType.OTHER:
        return "other"
    return None


def _write_output(stream, payload, line_end):
    destination = sys.stderr if stream == "stderr" else sys.stdout
    destination.write(payload)
    if line_end:
        destination.write("\n")
    destination.flush()
