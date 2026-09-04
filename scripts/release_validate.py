# SPDX-License-Identifier: Apache-2.0

"""Run the serial native-Linux V1 release-validation layers.

This driver is intentionally separate from pytest discovery.  It provides a
repeatable, fail-fast command sequence and makes missing external evidence
explicit instead of silently converting it to a skip.  Hardware values remain
in the ignored inventory supplied by the operator.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFERRED_GATES = ("PG-012", "PG-013")
REQUIRED_CAPABILITIES = frozenset(
    {"flash", "debug", "attach", "debugserver", "thread_info", "rtt", "semihosting"}
)


@dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...]
    environment: dict[str, str] | None = None


@dataclass(frozen=True)
class StepResult:
    name: str
    command: tuple[str, ...]
    returncode: int
    output: str


def _python_module(module: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-m", module, *args)


def source_python_files(root: Path = Path(".")) -> tuple[str, ...]:
    """Select source files without depending on Git metadata."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return tuple(line for line in result.stdout.splitlines() if line)
    excluded = {".git", ".venv", ".scratch", ".pytest_cache", ".ruff_cache", "__pycache__"}
    return tuple(
        str(path) for path in sorted(root.rglob("*.py")) if not excluded.intersection(path.parts)
    )


def build_steps(args: argparse.Namespace) -> list[Step]:
    """Build the ordered release steps from explicit prerequisites."""
    steps = [Step("pytest", _python_module("pytest", "-q"))]
    for name, command in (
        ("ruff", ("ruff", "check", ".")),
        ("ruff-format", ("ruff", "format", "--check", ".")),
        (
            "pylint",
            ("pylint", "--rcfile=pylintrc", *args.python_files),
        ),
        (
            "vermin",
            (
                "vermin",
                "-f",
                "parsable",
                "--violations",
                "-t=3.12-",
                "--no-make-paths-absolute",
                *args.python_files,
            ),
        ),
    ):
        steps.append(Step(name, command))

    if args.zephyr_base and args.west and args.board:
        environment = os.environ.copy()
        environment.update(
            ZEPHYR_BASE=str(args.zephyr_base), WEST=str(args.west), OPENOCD_TEST_BOARD=args.board
        )
        environment["ZRO_STRICT_EXTERNAL"] = "1"
        steps.append(
            Step(
                "zephyr",
                _python_module("pytest", "-q", "tests/zephyr_integration"),
                environment,
            )
        )
    else:
        raise ValueError("strict validation requires --zephyr-base, --west, and --board")

    if not args.hardware_config:
        raise ValueError("strict validation requires --hardware-config")
    inventory = str(args.hardware_config)
    external_environment = os.environ.copy()
    external_environment["ZRO_STRICT_EXTERNAL"] = "1"
    steps.extend(
        (
            Step(
                "ssh",
                _python_module(
                    "pytest",
                    "-q",
                    "tests/ssh_integration",
                    "-m",
                    "ssh",
                    "-k",
                    "not TestWslSshIntegration",
                    "--hardware-config",
                    inventory,
                ),
                external_environment,
            ),
            Step(
                "hardware",
                _python_module(
                    "pytest",
                    "-q",
                    "tests/hardware",
                    "-m",
                    "hardware",
                    "--hardware-config",
                    inventory,
                ),
                external_environment,
            ),
        )
    )
    if not (args.benchmark_build_dir and args.benchmark_config and args.benchmark_cwd):
        raise ValueError(
            "strict validation requires --benchmark-build-dir, --benchmark-config, "
            "and --benchmark-cwd"
        )
    steps.append(
        Step(
            "benchmark",
            _python_module(
                "tests.startup_benchmark",
                "--build-dir",
                str(args.benchmark_build_dir),
                "--config",
                str(args.benchmark_config),
                "--cwd",
                str(args.benchmark_cwd),
                "--west",
                str(args.west),
                "--command",
                args.benchmark_command,
                "--warmup",
                str(args.benchmark_warmup),
                "--iterations",
                str(args.benchmark_iterations),
            ),
        )
    )
    return steps


def validate_inputs(args: argparse.Namespace) -> None:
    """Reject missing strict-release prerequisites before running destructive steps."""
    if args.hardware_config is None or not args.hardware_config.is_file():
        raise ValueError("strict validation requires an existing --hardware-config")
    if args.zephyr_base is None or not args.zephyr_base.is_dir():
        raise ValueError("strict validation requires an existing --zephyr-base directory")
    if args.west is None or not args.west.is_file():
        raise ValueError("strict validation requires an executable --west path")
    if not os.access(args.west, os.X_OK):
        raise ValueError(f"west is not executable: {args.west}")
    for label, path in (
        ("benchmark-build-dir", args.benchmark_build_dir),
        ("benchmark-config", args.benchmark_config),
        ("benchmark-cwd", args.benchmark_cwd),
    ):
        if path is None:
            continue
        expected_directory = label != "benchmark-config"
        exists = path.is_dir() if expected_directory else path.is_file()
        if not exists:
            kind = "directory" if expected_directory else "file"
            raise ValueError(f"{label} must name an existing {kind}: {path}")
    for tool in ("ruff", "pylint", "vermin"):
        if shutil.which(tool) is None:
            raise ValueError(f"strict validation requires {tool!r} on PATH")


def inventory_capabilities(path: Path) -> dict[str, object]:
    """Return required and advertised capability names for release reporting."""
    from tests.inventory import load_inventory

    inventory = load_inventory(path)
    advertised = {
        capability
        for target in inventory.targets
        for profile in target.profiles
        for capability in profile.capabilities
    }
    missing = sorted(REQUIRED_CAPABILITIES - advertised)
    return {
        "required": sorted(REQUIRED_CAPABILITIES),
        "advertised": sorted(advertised),
        "missing": missing,
        "pass": not missing,
    }


def validate_inventory_capabilities(path: Path) -> None:
    """Require the configured release inventory to advertise V1 evidence."""
    missing = inventory_capabilities(path)["missing"]
    if missing:
        raise ValueError(
            "hardware inventory is missing required capabilities: " + ", ".join(missing)
        )


def benchmark_result(output: str) -> dict[str, object] | None:
    """Decode the benchmark's first JSON object from combined process output."""
    start = output.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(output[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def run_steps(steps: list[Step], executor=subprocess.run) -> list[StepResult]:
    """Execute steps serially, returning results and stopping at first failure."""
    results: list[StepResult] = []
    for step in steps:
        completed = executor(
            list(step.command),
            capture_output=True,
            text=True,
            check=False,
            env=step.environment,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        result = StepResult(step.name, step.command, completed.returncode, output)
        results.append(result)
        if completed.returncode:
            break
    return results


def local_leak_scan() -> dict[str, object]:
    """Report local helper/forwarding processes still visible after validation."""
    if shutil.which("ps") is None:
        return {"available": False, "clean": None, "matches": []}
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False
    )
    current = str(os.getpid())
    matches = []
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) < 2 or fields[0] == current:
            continue
        if any(token in fields[1] for token in ("remote_openocd", "remote-openocd", "helper.py")):
            matches.append(line.strip())
    return {"available": result.returncode == 0, "clean": not matches, "matches": matches}


def remote_leak_scan(inventory_path: Path) -> dict[str, object]:
    """Check each inventory host for helper/OpenOCD processes after the run."""
    try:
        from tests.inventory import load_inventory

        inventory = load_inventory(inventory_path)
    except Exception as error:  # pragma: no cover - validation already reports details
        return {"available": False, "clean": False, "error": str(error), "hosts": []}
    hosts = []
    for host in inventory.hosts:
        command = [
            *host.ssh_command,
            host.address,
            "pgrep -af 'remote-openocd|remote_helper|helper.py|openocd' || true",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        hosts.append({"host": host.id, "available": result.returncode == 0, "matches": lines})
    return {
        "available": all(item["available"] for item in hosts),
        "clean": all(not item["matches"] for item in hosts),
        "hosts": hosts,
    }


def _metadata() -> dict[str, str]:
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "revision": "unknown",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware-config", type=Path)
    parser.add_argument("--zephyr-base", type=Path)
    parser.add_argument("--west", type=Path)
    parser.add_argument("--board")
    parser.add_argument("--benchmark-build-dir", type=Path)
    parser.add_argument("--benchmark-config", type=Path)
    parser.add_argument("--benchmark-cwd", type=Path)
    parser.add_argument("--benchmark-command", default="flash")
    parser.add_argument("--benchmark-warmup", type=int, default=5)
    parser.add_argument("--benchmark-iterations", type=int, default=100)
    parser.add_argument(
        "--python-files",
        nargs="*",
        default=(),
        help="tracked Python files to pass to Pylint and Vermin",
    )
    args = parser.parse_args(argv)
    if not args.python_files:
        args.python_files = source_python_files()
    try:
        validate_inputs(args)
        capability_report = inventory_capabilities(args.hardware_config)
        validate_inventory_capabilities(args.hardware_config)
        steps = build_steps(args)
    except ValueError as error:
        parser.error(str(error))
    results = run_steps(steps)
    performance = next(
        (benchmark_result(result.output) for result in results if result.name == "benchmark"),
        None,
    )
    summary = {
        "schema": "zro.release-validation.v1",
        "metadata": _metadata(),
        "capabilities": capability_report,
        "performance": performance,
        "steps": [
            {
                "name": result.name,
                "command": list(result.command),
                "returncode": result.returncode,
            }
            for result in results
        ],
        "deferred": list(DEFERRED_GATES),
        "local_leaks": local_leak_scan(),
        "remote_leaks": remote_leak_scan(args.hardware_config),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    failed = next((result for result in results if result.returncode), None)
    if failed:
        print(f"{failed.name} failed:\n{failed.output}", file=sys.stderr)
        return failed.returncode or 1
    if summary["local_leaks"]["clean"] is False or summary["remote_leaks"]["clean"] is False:
        print("process leak detected", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
