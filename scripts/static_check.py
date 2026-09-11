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


def yaml_files(root: Path) -> tuple[str, ...]:
    """Return tracked YAML files, including the canonical example."""
    result = subprocess.run(
        (
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.yaml",
            "*.yml",
            "*.yaml.example",
        ),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(result.stdout.splitlines())


def commands(
    python_files: tuple[str, ...], yaml_paths: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    """Build the ordered static-check commands."""
    python = sys.executable
    schema = "python/zephyr_remote_openocd/resources/configuration.schema.json"
    example = "resources/config.yaml.example"
    workflow_paths = tuple(
        path for path in yaml_paths if Path(path).parts[:2] == (".github", "workflows")
    )
    return (
        (python, "-m", "ruff", "check", "."),
        (python, "-m", "ruff", "format", "--check", "."),
        (python, "-m", "mypy", "--config-file=mypy.ini", *python_files),
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
        (python, "-m", "yamllint", "-c", ".yamllint", *yaml_paths),
        (tool_executable("check-jsonschema"), "--check-metaschema", schema),
        (
            tool_executable("check-jsonschema"),
            "--schemafile",
            schema,
            "--force-filetype",
            "yaml",
            example,
        ),
        (tool_executable("actionlint"), "-no-color", *workflow_paths),
        ("git", "diff", "--check", "HEAD"),
    )


def main() -> int:
    """Run checks in order and stop after the first failure."""
    root = repository_root()
    for command in commands(source_files(root), yaml_files(root)):
        result = subprocess.run(command, cwd=root, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
