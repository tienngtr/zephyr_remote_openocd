# SPDX-License-Identifier: Apache-2.0

"""Run local tests and report coverage and working-tree complexity."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def repository_root() -> Path:
    result = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-revision",
        help="committed revision used as the complexity comparison base",
    )
    parser.add_argument(
        "--zephyr-base",
        type=Path,
        help="also run the Zephyr adapter tests using this Zephyr 4.4 source tree",
    )
    return parser.parse_args(argv)


def commands(args: argparse.Namespace) -> list[tuple[tuple[str, ...], dict[str, str] | None]]:
    python = sys.executable
    result: list[tuple[tuple[str, ...], dict[str, str] | None]] = [
        (
            (
                python,
                "-m",
                "pytest",
                "--cov",
                "--cov-config=.coveragerc",
                "--cov-report=",
            ),
            None,
        )
    ]
    if args.zephyr_base is not None:
        environment = os.environ.copy()
        environment["ZEPHYR_BASE"] = str(args.zephyr_base.resolve())
        result.append(
            (
                (
                    python,
                    "-m",
                    "pytest",
                    "tests/zephyr_integration/test_adapter.py",
                    "-m",
                    "zephyr",
                    "--cov",
                    "--cov-config=.coveragerc",
                    "--cov-append",
                    "--cov-report=",
                ),
                environment,
            )
        )
    result.append(((python, "scripts/coverage_summary.py"), None))
    complexity = [python, "scripts/radon_summary.py", "--working-tree"]
    if args.base_revision:
        complexity.extend(("--base-revision", args.base_revision))
    result.append((tuple(complexity), None))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = repository_root()
    for command, environment in commands(args):
        result = subprocess.run(command, cwd=root, env=environment, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
