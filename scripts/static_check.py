# SPDX-License-Identifier: Apache-2.0

"""Run all repository static checks with one command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def repository_root() -> Path:
    """Ask Git for the repository containing this script."""
    result = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        cwd=Path(__file__).resolve().parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def tool_executable(name: str) -> str:
    """Prefer a console script installed beside the active Python."""
    adjacent = Path(sys.executable).with_name(name)
    return str(adjacent) if adjacent.is_file() else name


def source_files(root: Path) -> tuple[str, ...]:
    """Return tracked Python files relative to the repository root."""
    result = subprocess.run(
        ("git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(result.stdout.splitlines())


def commands(python_files: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Build the ordered static-check commands."""
    python = sys.executable
    return (
        (python, "-m", "ruff", "check", "."),
        (python, "-m", "ruff", "format", "--check", "."),
        (python, "-m", "pylint", "-j", "1", "--rcfile=pylintrc", *python_files),
        (
            tool_executable("vermin"),
            "-p=1",
            "-f",
            "parsable",
            "--violations",
            "-t=3.12-",
            "--no-make-paths-absolute",
            *python_files,
        ),
        ("git", "diff", "--check", "HEAD"),
    )


def main() -> int:
    """Run checks in order and stop after the first failure."""
    root = repository_root()
    for command in commands(source_files(root)):
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
