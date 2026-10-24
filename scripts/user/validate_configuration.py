#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Validate and summarize user configuration without external I/O."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

from zephyr_remote_openocd.config import (  # noqa: E402
    ConfigError,
    RemoteOpenOcdConfig,
    ResolvedRemote,
    default_config_path,
    load_config,
    resolve_remote,
)


def _names(values: Iterable[str]) -> str:
    names = list(values)
    return ", ".join(names) if names else "(none)"


def _print_config(config: RemoteOpenOcdConfig) -> None:
    print(f"Configuration valid: {config.path}")
    print(f"Default runner: {config.default_runner}")
    print(f"Presets: {_names(sorted(config.presets))}")
    print(f"Remotes: {_names(sorted(config.remotes))}")


def _print_remote(remote: ResolvedRemote) -> None:
    print(f"Resolved remote: {remote.name}")
    print(f"  SSH host: {remote.ssh_host}")
    print(f"  SSH command: {json.dumps(list(remote.ssh_command))}")
    print(f"  OpenOCD command: {json.dumps(list(remote.openocd_command))}")
    print(f"  Forwarded environment names: {_names(remote.forward_env)}")
    if remote.path_mappings:
        print("  Path mappings:")
        for mapping in remote.path_mappings:
            print(f"    {mapping.local} -> {mapping.remote}")
    else:
        print("  Path mappings: (none)")


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and summarize remote OpenOCD configuration without external I/O."
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        help="configuration path (default: product configuration path)",
    )
    parser.add_argument(
        "--remote",
        metavar="NAME",
        help="remote to resolve (default: default_remote from the file)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    path = (args.config or default_config_path()).expanduser()
    try:
        path.lstat()
    except FileNotFoundError:
        print(
            f"Configuration invalid: {path} does not exist; run "
            "python3 scripts/user/setup.py or provide CONFIG",
            file=sys.stderr,
        )
        return 1
    except OSError:
        # Let load_config preserve the actionable inspection or read failure.
        pass

    try:
        config = load_config(path)
        selected = args.remote if args.remote is not None else config.default_remote
        remote = resolve_remote(config, selected) if selected is not None else None
    except ConfigError as error:
        print(f"Configuration invalid: {error}", file=sys.stderr)
        return 1

    _print_config(config)
    if remote is None:
        print("No default remote is configured.")
        print("Resolve one with:")
        print("  python3 scripts/user/validate_configuration.py [CONFIG] --remote NAME")
    else:
        _print_remote(remote)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
