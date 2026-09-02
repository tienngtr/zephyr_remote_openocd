"""Fake Zephyr 4.4 runner used to validate integration boundaries."""

from __future__ import annotations

import json
import os

from runners.openocd import OpenOcdBinaryRunner

from zephyr_remote_openocd.config import ConfigError, load_config


class RemoteOpenOcdBinaryRunner(OpenOcdBinaryRunner):
    """OpenOCD-compatible runner which deliberately performs no I/O."""

    def __init__(self, cfg, parsed_args):
        # Reuse OpenOCD's public initializer and the state it constructs. The
        # prototype never calls its process-launching implementation.
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
            image_type=cfg.file_type,
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
        if os.environ.get("ZEPHYR_REMOTE_OPENOCD_RECORD") != "1":
            raise RuntimeError(
                "remote-openocd execution is not implemented; "
                "the recording backend is available only to integration tests"
            )
        try:
            selected = load_config()
        except ConfigError as error:
            raise RuntimeError(str(error)) from error

        runner_args = {
            key: value
            for key, value in vars(self.prototype_args).items()
            if value not in (None, False, "", [])
        }
        common = {
            key: _json_value(getattr(self.prototype_cfg, key, None))
            for key in (
                "board_dir", "elf_file", "hex_file", "bin_file", "file",
                "file_type", "gdb", "openocd", "openocd_search",
            )
        }
        payload = {
            "prototype": True,
            "runner": self.name(),
            "command": command,
            "runner_args": runner_args,
            "runner_config": common,
            "selected_config": selected.printable(),
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _json_value(value):
    if hasattr(value, "value"):
        return value.value
    return value
