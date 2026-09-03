# SPDX-License-Identifier: Apache-2.0

"""Zephyr 4.4 adapter for recording and remote OpenOCD operations."""

from __future__ import annotations

import json
import os
import secrets
import sys

from runners.core import FileType  # pylint: disable=no-name-in-module
from runners.openocd import OpenOcdBinaryRunner  # pylint: disable=no-name-in-module

from zephyr_remote_openocd.config import ConfigError, load_config, require_remote_settings
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
        return "remote-openocd"

    @classmethod
    def do_create(cls, cfg, args):
        return cls(cfg, args)

    def do_run(self, command, **kwargs):
        try:
            selected = load_config()
        except ConfigError as error:
            raise RuntimeError(str(error)) from error

        if os.environ.get("ZEPHYR_REMOTE_OPENOCD_RECORD") == "1":
            _record_runner(self, command, selected)
            return
        try:
            if command == "flash":
                request = _flash_request(self, selected)
                plan = None
                backend = SshHelperBackend(output_handler=_write_output)
            elif command in {"debug", "attach", "debugserver", "rtt"}:
                backend = SshHelperBackend(output_handler=_write_output)
                version = None
                if self.thread_info_enabled:
                    version = parse_openocd_version(
                        backend.openocd_version(
                            SshCommand(selected.ssh_command),
                            selected.remote_host,
                            _remote_openocd(selected, command),
                        )
                    )
                plan = _debug_plan(self, command, selected, version)
                request = _debug_request(self, selected, plan)
            else:
                raise RuntimeError(f"remote-openocd {command} is not implemented")
        except (
            ConfigError,
            FlashPlanError,
            DebugPlanError,
            PathPlanningError,
            RttClientError,
        ) as error:
            raise RuntimeError(str(error)) from error
        session = RemoteSession(request, backend)
        try:
            descriptor = session.start()
            self.logger.info(
                "Remote OpenOCD session %s workspace=%s bindto=%s",
                descriptor.session_id,
                descriptor.remote_workspace,
                descriptor.remote_address,
            )
            if command != "rtt" and plan is not None and plan.rtt_service is not None:
                self.logger.info(
                    "Remote OpenOCD RTT server available at 127.0.0.1:%s",
                    plan.rtt_service.local_port,
                )
            if command == "debugserver":
                assert plan is not None
                gdb = next(item for item in plan.services if item.name == "gdb")
                self.logger.info(
                    "Remote OpenOCD GDB server available at 127.0.0.1:%s",
                    gdb.local_port,
                )
            if command == "rtt":
                assert plan is not None and plan.gdb_argv is not None
                assert plan.rtt_service is not None
                self.require(plan.gdb_argv[0])
                try:
                    self.run_client(list(plan.gdb_argv))
                    session.forward((plan.rtt_service,))
                    self.logger.info(
                        "Remote OpenOCD RTT server available at 127.0.0.1:%s",
                        plan.rtt_service.local_port,
                    )
                    returncode = run_rtt_client(
                        plan.rtt_service.local_port,
                        session.poll,
                    )
                finally:
                    session.close()
                if returncode:
                    raise RuntimeError(f"remote OpenOCD failed with exit status {returncode}")
                return
            if command in {"debug", "attach"}:
                assert plan is not None and plan.gdb_argv is not None
                self.require(plan.gdb_argv[0])
                try:
                    self.run_client(list(plan.gdb_argv))
                finally:
                    returncode = session.poll()
                    session.close()
                if returncode:
                    raise RuntimeError(f"remote OpenOCD failed with exit status {returncode}")
                return
            returncode = session.wait()
        except KeyboardInterrupt:
            session.close()
            raise
        if returncode:
            raise RuntimeError(f"remote OpenOCD failed with exit status {returncode}")


def _record_runner(runner, command, selected):
    runner_args = {
        key: value
        for key, value in vars(runner.parsed_args).items()
        if value not in (None, False, "", [])
    }
    common = {
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
    request = None
    local_gdb = None
    thread_info = None
    rtt = None
    if command == "flash" and selected.remote_host and selected.remote_openocd:
        request = _flash_request(runner, selected)
    elif (
        command in {"debug", "attach", "debugserver", "rtt"}
        and selected.remote_host
        and selected.remote_openocd
    ):
        requested = runner.thread_info_enabled
        supplied = os.environ.get("ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION")
        if requested and supplied is None:
            raise RuntimeError(
                "ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION is required to record a "
                "thread-info-enabled build"
            )
        version = parse_openocd_version(supplied) if requested else None
        plan = _debug_plan(runner, command, selected, version)
        request = _debug_request(runner, selected, plan)
        local_gdb = list(plan.gdb_argv) if plan.gdb_argv is not None else None
        thread_info = {
            "requested": requested,
            "version": supplied if requested else None,
            "version_source": "injected" if requested else None,
            "rtos_awareness": plan.rtos_awareness,
        }
        rtt = {
            "enabled": plan.rtt_service is not None,
            "address": runner.get_rtt_address() if plan.rtt_service is not None else None,
            "port": plan.rtt_service.local_port if plan.rtt_service is not None else None,
            "setup": plan.rtt_setup,
            "service_phase": ("deferred" if command == "rtt" else "initial")
            if plan.rtt_service is not None
            else None,
            "launches_local_client": plan.launches_rtt_client,
        }
    payload = {
        "recording": True,
        "runner": runner.name(),
        "command": command,
        "runner_args": runner_args,
        "runner_config": common,
        "selected_config": selected.printable(),
        "remote_session_request": _request_record(request) if request is not None else None,
        "local_gdb_argv": local_gdb,
        "thread_info": thread_info,
        "rtt": rtt,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


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
        plan.staged_files,
        (),
        plan.process,
    )


def _remote_openocd(selected, command):
    _, executable = require_remote_settings(selected, command)
    return executable


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
        plan.staged_files,
        plan.services,
        plan.process,
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
            "kind": request.process.kind,
            "argv": list(request.process.argv),
            "environment": [name for name, _ in request.process.environment],
            "required_paths": [
                {"path": item.path, "kind": item.kind} for item in request.process.required_paths
            ],
            "readiness_marker": request.process.readiness_marker,
            "readiness_timeout": request.process.readiness_timeout,
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


def _write_output(stream, payload):
    print(payload, file=sys.stderr if stream == "stderr" else sys.stdout, flush=True)
