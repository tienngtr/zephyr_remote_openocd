# SPDX-License-Identifier: Apache-2.0

"""Validate and summarize a local hardware inventory without external I/O."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.inventory import Inventory, InventoryError, load_inventory  # noqa: E402


def local_errors(inventory: Inventory) -> list[str]:
    """Return actionable failures for paths used on the contributor's machine."""
    errors: list[str] = []
    for environment in inventory.build_environments:
        if not environment.zephyr_base.is_dir():
            errors.append(
                f"build_environments.{environment.name}.zephyr_base: "
                f"directory does not exist: {environment.zephyr_base}"
            )
        if not environment.west.is_file() or not os.access(environment.west, os.X_OK):
            errors.append(
                f"build_environments.{environment.name}.west: "
                f"executable does not exist: {environment.west}"
            )
    for toolchain in inventory.toolchains:
        if not toolchain.gdb.is_file() or not os.access(toolchain.gdb, os.X_OK):
            errors.append(
                f"toolchains.{toolchain.name}.gdb: executable does not exist: {toolchain.gdb}"
            )
    for host in inventory.hosts:
        for mapping in host.path_mappings:
            if not mapping.local.exists():
                errors.append(
                    f"hosts.{host.name}.path_mappings: local path does not exist: {mapping.local}"
                )
    for target in inventory.targets:
        environment = inventory.build_environment(target.build_environment)
        for build in target.builds:
            application = Path(build.application)
            if not application.is_absolute():
                application = environment.zephyr_base / application
            if not application.is_dir():
                errors.append(
                    f"targets.{target.name}.builds.{build.name}.application: "
                    f"directory does not exist: {application}"
                )
    return errors


def summary(inventory: Inventory) -> str:
    """Return a stable, human-readable operation summary."""
    lines = [f"Inventory valid: {inventory.path}", "Targets:"]
    for target in inventory.targets:
        lines.append(f"  {target.name}:")
        for profile in target.profiles:
            lines.append(f"    {profile.name}: {', '.join(profile.operation_names)}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate a hardware inventory without contacting external resources."
    )
    parser.add_argument("inventory", type=Path, help="path to the local hardware inventory")
    parser.add_argument(
        "--check-local",
        action="store_true",
        help="also check local Zephyr, application, west, GDB, and mapping paths",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Validate the selected inventory and report failures on stderr."""
    args = parse_args(argv)
    try:
        inventory = load_inventory(args.inventory)
    except InventoryError as error:
        print(error, file=sys.stderr)
        return 1
    print(summary(inventory))
    if not args.check_local:
        return 0
    failures = local_errors(inventory)
    if failures:
        for failure in failures:
            print(f"Local check failed: {failure}", file=sys.stderr)
        return 1
    print("Local paths valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
