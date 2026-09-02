"""Zephyr 4.4 adapter for recording and remote OpenOCD flash."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from runners.core import FileType
from runners.openocd import OpenOcdBinaryRunner

from zephyr_remote_openocd.config import ConfigError, load_config
from zephyr_remote_openocd.remote import RemoteSession, RemoteSessionRequest, SshHelperBackend
from zephyr_remote_openocd.remote.flash import FlashInputs, FlashPlanError, build_flash_plan
from zephyr_remote_openocd.remote.paths import PathPlanner, PathPlanningError
from zephyr_remote_openocd.remote.ssh import SshCommand


class RemoteOpenOcdBinaryRunner(OpenOcdBinaryRunner):
    """OpenOCD-compatible runner with a version-isolated flash adapter."""

    def __init__(self, cfg, parsed_args):
        # Reuse OpenOCD's public initializer and state construction, but never
        # call its local process-launching implementation.
        image_type = cfg.file_type
        if parsed_args.use_image_type and (image_type == FileType.OTHER or image_type is None):
            image_type = {"elf": FileType.ELF, "hex": FileType.HEX, "bin": FileType.BIN}[parsed_args.use_image_type]
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
        self.prototype_cfg = cfg
        self.prototype_args = parsed_args

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
        if command != "flash":
            raise RuntimeError(f"remote-openocd {command} is not implemented; only flash is currently supported")
        try:
            request = _flash_request(self, selected)
        except (ConfigError, FlashPlanError, PathPlanningError) as error:
            raise RuntimeError(str(error)) from error
        session = RemoteSession(request, SshHelperBackend(output_handler=_write_output))
        try:
            descriptor = session.start()
            self.logger.info(
                "Remote OpenOCD session %s workspace=%s bindto=%s",
                descriptor.session_id,
                descriptor.remote_workspace,
                descriptor.remote_address,
            )
            returncode = session.wait()
        except KeyboardInterrupt:
            session.close()
            raise
        if returncode:
            raise RuntimeError(f"remote OpenOCD failed with exit status {returncode}")

def _record_runner(runner, command, selected):
    runner_args = {
        key: value
        for key, value in vars(runner.prototype_args).items()
        if value not in (None, False, "", [])
    }
    common = {
        key: _json_value(getattr(runner.prototype_cfg, key, None))
        for key in (
            "board_dir", "elf_file", "hex_file", "bin_file", "file",
            "file_type", "gdb", "openocd", "openocd_search",
        )
    }
    payload = {
        "prototype": True,
        "runner": runner.name(),
        "command": command,
        "runner_args": runner_args,
        "runner_config": common,
        "selected_config": selected.printable(),
        "remote_session_request": _request_record(_flash_request(runner, selected))
        if command == "flash" and selected.remote_host and selected.remote_openocd else None,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

def _flash_request(runner, selected):
    if not selected.remote_host:
        raise ConfigError(f"remote.host is required for remote flash ({selected.path})")
    if not selected.remote_openocd:
        raise ConfigError(f"remote.openocd is required for remote flash ({selected.path})")
    if not Path(selected.remote_openocd).is_absolute():
        raise ConfigError(f"remote.openocd must be an absolute path ({selected.path})")
    environment = []
    for name in selected.forward_env:
        value = os.environ.get(name)
        if value is None:
            runner.logger.warning("allow-listed environment variable %s is absent; omitting it", name)
        else:
            environment.append((name, value))
    search_paths = tuple(
        runner.openocd_cmd[index + 1]
        for index, argument in enumerate(runner.openocd_cmd[:-1]) if argument == "-s"
    )
    inputs = FlashInputs(
            executable=selected.remote_openocd,
            image_type=_file_type(runner.image_type),
            file=runner.prototype_cfg.file,
            elf_file=runner.prototype_cfg.elf_file,
            hex_file=runner.prototype_cfg.hex_file,
            bin_file=runner.prototype_cfg.bin_file,
            search_paths=search_paths,
            config_files=tuple(runner.openocd_config or ()),
            pre_init=tuple(runner.pre_init), reset_halt=runner.reset_halt_cmd,
            pre_load=tuple(runner.pre_load), erase_commands=tuple(runner.erase_cmd or ()),
            load_command=runner.load_cmd, verify_command=runner.verify_cmd,
            post_verify=tuple(runner.post_verify), verify=runner.do_verify,
            verify_only=runner.do_verify_only, erase=runner.do_erase,
            serial=runner.prototype_args.serial or None,
            flash_address=runner.flash_address,
            no_init=runner.prototype_args.no_init, no_targets=runner.prototype_args.no_targets,
    )
    planner = PathPlanner(selected.path_mappings)
    plan = build_flash_plan(inputs, planner, tuple(environment))
    return RemoteSessionRequest(
        selected.remote_host, SshCommand(selected.ssh_command),
        plan.staged_files, (), plan.process,
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
