#!/usr/bin/env python3
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
        cwd=Path(__file__).resolve(strict=True).parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve(strict=True)


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


def shell_files(root: Path) -> tuple[str, ...]:
    """Return existing tracked or untracked shell scripts."""
    result = subprocess.run(
        ("git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.sh"),
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


NONSTRICT_PATH_MARKER = "# nonstrict-path:"


def _strict_argument(call: ast.Call) -> bool:
    """Return whether a call passes strict=True by keyword or position."""
    keywords = (keyword.value for keyword in call.keywords if keyword.arg == "strict")
    return any(
        isinstance(argument, ast.Constant) and argument.value is True
        for argument in (*call.args, *keywords)
    )


def _justified(lines: list[str], call: ast.Call) -> bool:
    """Find the marker on the call or in the comment block directly above it."""
    end = call.end_lineno or call.lineno
    if any(NONSTRICT_PATH_MARKER in line for line in lines[call.lineno - 1 : end]):
        return True
    index = call.lineno - 2
    while index >= 0 and lines[index].lstrip().startswith("#"):
        if NONSTRICT_PATH_MARKER in lines[index]:
            return True
        index -= 1
    return False


def _path_module_bindings(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Return expressions naming os.path and local names bound to its functions."""
    modules = {"os.path"}
    functions = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os" and alias.asname:
                    modules.add(f"{alias.asname}.path")
                elif alias.name in ("os.path", "posixpath"):
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if node.module == "os" and alias.name == "path":
                    modules.add(alias.asname or alias.name)
                elif node.module in ("os.path", "posixpath"):
                    functions[alias.asname or alias.name] = alias.name
    return modules, functions


def _path_function(call: ast.Call, modules: set[str], functions: dict[str, str]) -> str | None:
    """Return the os.path function a call invokes, if any."""
    if isinstance(call.func, ast.Name):
        return functions.get(call.func.id)
    if isinstance(call.func, ast.Attribute) and ast.unparse(call.func.value) in modules:
        return call.func.attr
    return None


def check_path_semantics(root: Path, paths: tuple[str, ...]) -> bool:
    """Reject local path resolution that may apply '..' lexically."""
    valid = True
    for relative_path in paths:
        if Path(relative_path).parts[0] == "tests":
            continue
        source = (root / relative_path).read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=relative_path)
        modules, functions = _path_module_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = _path_function(node, modules, functions)
            if function == "realpath" or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "resolve"
            ):
                lexical = not _strict_argument(node)
            else:
                lexical = function in ("abspath", "normpath")
            if lexical and not _justified(lines, node):
                print(
                    f"Lexical local path resolution: {relative_path}:{node.lineno}; resolve "
                    f"strictly or justify it with a '{NONSTRICT_PATH_MARKER}' comment",
                    file=sys.stderr,
                )
                valid = False
    return valid


def commands(
    python_files: tuple[str, ...],
    yaml_paths: tuple[str, ...],
    markdown_paths: tuple[str, ...],
    shell_paths: tuple[str, ...],
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
            (
                python,
                "scripts/contributor/validate_hardware_inventory.py",
                ci_hardware,
            ),
        ),
        (
            "shellcheck",
            (tool_executable("shellcheck"), *shell_paths),
        ),
        (
            "actionlint",
            # actionlint silently skips run: scripts when it cannot find
            # shellcheck, which is not on PATH for an unactivated virtualenv.
            (
                tool_executable("actionlint"),
                "-no-color",
                f"-shellcheck={tool_executable('shellcheck')}",
                *workflow_paths,
            ),
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
                # Agent definition files carry their title as front matter `name`.
                "--config",
                r"MD041.front-matter-title-pattern = '^\s*name\s*[:=]'",
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
    report_phase("Local path semantics check")
    if not check_path_semantics(root, python_files):
        return 1
    for name, command in commands(
        python_files, yaml_files(root), markdown_files(root), shell_files(root)
    ):
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
