# SPDX-License-Identifier: Apache-2.0

"""Adapt the public Zephyr 4.4 OpenOCD constructor and parser."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Self

from runners.core import FileType, RunnerConfig  # pylint: disable=no-name-in-module
from runners.openocd import OpenOcdBinaryRunner  # pylint: disable=no-name-in-module

from zephyr_remote_openocd._runner import run_operation


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
        run_operation(self, command)
