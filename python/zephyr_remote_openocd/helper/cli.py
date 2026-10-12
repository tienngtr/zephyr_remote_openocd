# SPDX-License-Identifier: Apache-2.0

"""Remote helper command dispatch and JSON-only error reporting."""

from __future__ import annotations

import argparse

from zephyr_remote_openocd.helper.control import control
from zephyr_remote_openocd.helper.staging import stage
from zephyr_remote_openocd.helper.streams import error
from zephyr_remote_openocd.helper.version import openocd_version


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("control")
    staging = sub.add_parser("stage")
    staging.add_argument("workspace")
    version = sub.add_parser("openocd-version")
    version.add_argument("executable", nargs="+")
    args = parser.parse_args()
    if args.command == "control":
        control()
    elif args.command == "stage":
        stage(args.workspace)
    elif args.command == "openocd-version":
        openocd_version(args.executable)


def run() -> None:
    try:
        main()
    except Exception as exc:
        error(exc)
        raise SystemExit(1) from exc
