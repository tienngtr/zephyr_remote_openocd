# SPDX-License-Identifier: Apache-2.0

"""Run all repository static checks with one command."""

from __future__ import annotations

import ast
import json
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


def report_phase(name: str) -> None:
    """Print the check currently running, even when output is piped."""
    print(f"Running {name}...", flush=True)


def source_files(root: Path) -> tuple[str, ...]:
    """Return existing tracked or untracked Python files."""
    result = subprocess.run(
        ("git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(path for path in result.stdout.splitlines() if (root / path).is_file())


def yaml_files(root: Path) -> tuple[str, ...]:
    """Return existing tracked or untracked YAML files."""
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
        ),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(path for path in result.stdout.splitlines() if (root / path).is_file())


def markdown_files(root: Path) -> tuple[str, ...]:
    """Return existing tracked or untracked Markdown files."""
    result = subprocess.run(
        ("git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(path for path in result.stdout.splitlines() if (root / path).is_file())


def json_schema_files() -> tuple[str, ...]:
    """Return repository JSON schemas that use the canonical formatting."""
    return (
        "python/zephyr_remote_openocd/resources/configuration.schema.json",
        "tests/fixtures/hardware.schema.json",
    )


def check_json_format(root: Path, paths: tuple[str, ...]) -> bool:
    """Check that JSON files use two-space indentation and end with a newline."""
    valid = True
    for relative_path in paths:
        path = root / relative_path
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"JSON formatting check failed for {relative_path}: {error}", file=sys.stderr)
            valid = False
            continue
        expected = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            print(
                f"JSON formatting check failed for {relative_path}; use this command "
                "template: python3 -m json.tool --indent 2 INPUT.json > "
                "OUTPUT.json.tmp && mv OUTPUT.json.tmp OUTPUT.json",
                file=sys.stderr,
            )
            valid = False
    return valid


def check_ssh_config(root: Path) -> bool:
    """Validate the committed OpenSSH client configuration."""
    result = subprocess.run(
        (
            "ssh",
            "-G",
            "-F",
            "tests/ci/ssh/ssh_config",
            "ci-ssh",
        ),
        cwd=root,
        stdout=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def check_zephyr_import_boundary(root: Path, paths: tuple[str, ...]) -> bool:
    """Keep upstream OpenOCD runner imports in the Zephyr compatibility layer."""
    package = Path("python/zephyr_remote_openocd")
    compatibility_layer = package / "zephyr44"
    valid = True
    for relative_path in paths:
        path = Path(relative_path)
        if package not in path.parents or compatibility_layer in path.parents:
            continue
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=relative_path)
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                modules = [module, *(f"{module}.{alias.name}" for alias in node.names)]
                lineno = node.lineno
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
                lineno = node.lineno
            else:
                continue
            if any(
                module == "runners.openocd" or module.startswith("runners.openocd.")
                for module in modules
            ):
                print(
                    "Zephyr OpenOCD coupling outside compatibility layer: "
                    f"{relative_path}:{lineno}",
                    file=sys.stderr,
                )
                valid = False
    return valid


def commands(
    python_files: tuple[str, ...],
    yaml_paths: tuple[str, ...],
    markdown_paths: tuple[str, ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Build the ordered static-check commands."""
    python = sys.executable
    schema, hardware_schema = json_schema_files()
    example = "resources/config.example.yaml"
    hardware_example = "tests/fixtures/hardware.example.yaml"
    hardware_complete_example = "tests/fixtures/hardware.complete.example.yaml"
    ci_hardware = "tests/ci/ssh/hardware.yaml"
    workflow_paths = tuple(
        path for path in yaml_paths if Path(path).parts[:2] == (".github", "workflows")
    )
    return (
        ("ruff check", (python, "-m", "ruff", "check", ".")),
        ("ruff format", (python, "-m", "ruff", "format", "--check", ".")),
        ("mypy", (python, "-m", "mypy", "--config-file=mypy.ini", *python_files)),
        (
            "pylint",
            (python, "-m", "pylint", "-j", "1", "--rcfile=pylintrc", *python_files),
        ),
        (
            "vermin",
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
        ),
        (
            "yamllint",
            (python, "-m", "yamllint", "-c", ".yamllint", *yaml_paths),
        ),
        (
            "check-jsonschema (configuration schema)",
            (tool_executable("check-jsonschema"), "--check-metaschema", schema),
        ),
        (
            "check-jsonschema (hardware schema)",
            (tool_executable("check-jsonschema"), "--check-metaschema", hardware_schema),
        ),
        (
            "check-jsonschema (configuration example)",
            (
                tool_executable("check-jsonschema"),
                "--schemafile",
                schema,
                "--force-filetype",
                "yaml",
                example,
            ),
        ),
        (
            "check-jsonschema (hardware example)",
            (
                tool_executable("check-jsonschema"),
                "--schemafile",
                hardware_schema,
                "--force-filetype",
                "yaml",
                hardware_example,
            ),
        ),
        (
            "check-jsonschema (complete hardware example)",
            (
                tool_executable("check-jsonschema"),
                "--schemafile",
                hardware_schema,
                "--force-filetype",
                "yaml",
                hardware_complete_example,
            ),
        ),
        (
            "hardware inventory validation",
            (python, "scripts/validate_hardware_inventory.py", ci_hardware),
        ),
        (
            "actionlint",
            (tool_executable("actionlint"), "-no-color", *workflow_paths),
        ),
        (
            "rumdl",
            (
                tool_executable("rumdl"),
                "check",
                "--no-config",
                "--no-cache",
                "--color",
                "never",
                "--flavor",
                "gfm",
                "--enable",
                "MD001,MD025,MD041,MD051,MD057",
                *markdown_paths,
            ),
        ),
        (
            "git diff check",
            ("git", "diff", "--check", "HEAD"),
        ),
    )


def main() -> int:
    """Run checks in order and stop after the first failure."""
    root = repository_root()
    python_files = source_files(root)
    report_phase("JSON schema formatting")
    if not check_json_format(root, json_schema_files()):
        return 1
    report_phase("OpenSSH client configuration check")
    if not check_ssh_config(root):
        return 1
    report_phase("Zephyr import boundary check")
    if not check_zephyr_import_boundary(root, python_files):
        return 1
    for name, command in commands(python_files, yaml_files(root), markdown_files(root)):
        report_phase(name)
        result = subprocess.run(
            command,
            cwd=root,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
