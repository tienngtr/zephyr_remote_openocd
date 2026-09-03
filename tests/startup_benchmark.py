# SPDX-License-Identifier: Apache-2.0

"""Release-only benchmark for remote-openocd startup overhead.

This module is intentionally not named ``test_*.py``: it is never part of
ordinary test discovery.  Run it from a Zephyr workspace with ``--build-dir``
and an external remote-openocd TOML configuration.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

BENCHMARK_VERSION = "1"
NORMATIVE_LIMIT_SECONDS = 0.5


def percentile(samples: list[float], fraction: float) -> float:
    """Return an interpolated inclusive percentile in a reproducible way."""
    if not samples:
        raise ValueError("at least one sample is required")
    if not 0 <= fraction <= 1:
        raise ValueError("percentile fraction must be between zero and one")
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def summarize(samples: list[float]) -> dict[str, float | int]:
    if not samples:
        raise ValueError("at least one sample is required")
    return {
        "first": samples[0],
        "median": statistics.median(samples),
        "p95": percentile(samples, 0.95),
        "worst": max(samples),
        "iterations": len(samples),
    }


def overhead_summary(baseline: list[float], remote: list[float]) -> dict[str, float | int]:
    """Summarize paired remote-minus-baseline observations."""
    if len(baseline) != len(remote) or not baseline:
        raise ValueError("paired non-empty samples are required")
    return summarize(
        [
            remote_value - base_value
            for base_value, remote_value in zip(baseline, remote, strict=True)
        ]
    )


def passes_threshold(samples: list[float], threshold: float) -> bool:
    """Apply the SRS threshold to the normative median statistic."""
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    return summarize(samples)["median"] < threshold


def _run(command: list[str], environment: dict[str, str], cwd: Path) -> float:
    started = time.perf_counter_ns()
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    if result.returncode:
        raise RuntimeError(
            f"benchmark command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return elapsed


def _measure(
    command: list[str], environment: dict[str, str], cwd: Path, warmup: int, iterations: int
) -> list[float]:
    for _ in range(warmup):
        _run(command, environment, cwd)
    return [_run(command, environment, cwd) for _ in range(iterations)]


def _metadata(root: Path, args: argparse.Namespace) -> dict[str, object]:
    try:
        revision = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                check=False,
                text=True,
            ).stdout.strip()
            or "unknown"
        )
    except OSError:
        revision = "unknown"
    cpu = "unknown"
    with contextlib.suppress(OSError):
        cpu = platform.processor() or "unknown"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "cpu": cpu,
        "revision": revision,
        "parameters": {
            "warmup": args.warmup,
            "iterations": args.iterations,
            "command": args.command,
            "build_dir": str(args.build_dir),
            "config": str(args.config),
        },
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    west = str(args.west)
    common = [west, args.command, "-d", str(args.build_dir), "--no-rebuild"]
    baseline_command = [*common, "-r", "openocd", "--context"]
    remote_command = [*common, "-r", "remote-openocd"]
    baseline_environment = os.environ.copy()
    baseline_environment["PYTHONPATH"] = str(root / "python")
    remote_environment = baseline_environment.copy()
    remote_environment.update(
        {
            "EXTRA_ZEPHYR_MODULES": str(root),
            "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(args.config),
            "ZEPHYR_REMOTE_OPENOCD_RECORD": "1",
        }
    )
    baseline = _measure(
        baseline_command, baseline_environment, args.cwd, args.warmup, args.iterations
    )
    remote = _measure(remote_command, remote_environment, args.cwd, args.warmup, args.iterations)
    deltas = [
        remote_value - baseline_value
        for baseline_value, remote_value in zip(baseline, remote, strict=True)
    ]
    result = {
        "benchmark_version": BENCHMARK_VERSION,
        "schema": "zro.startup-overhead.v1",
        "case": args.command,
        "baseline_treatment": (
            "built-in openocd --context is a conservative lower-bound proxy for "
            "the corresponding command path; it is not an exact execution comparison"
        ),
        "baseline": {"runner": "openocd", "statistics": summarize(baseline)},
        "remote": {"runner": "remote-openocd", "statistics": summarize(remote)},
        "overhead": {
            "statistics": summarize(deltas),
            "normative_statistic": "median",
            "threshold_seconds": NORMATIVE_LIMIT_SECONDS,
            "pass": passes_threshold(deltas, NORMATIVE_LIMIT_SECONDS),
        },
        "environment": _metadata(root, args),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--west", type=Path, default="west")
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument(
        "--command",
        choices=("flash", "debug", "attach", "debugserver", "rtt"),
        default="flash",
    )
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    if args.warmup < 0 or args.iterations < 1:
        parser.error("warmup must be non-negative and iterations must be positive")
    result = run_benchmark(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(
        f"{args.command}: median additional startup "
        f"{result['overhead']['statistics']['median'] * 1000:.3f} ms "
        f"(p95={result['overhead']['statistics']['p95'] * 1000:.3f} ms, "
        f"worst={result['overhead']['statistics']['worst'] * 1000:.3f} ms)",
        file=sys.stderr,
    )
    return 0 if result["overhead"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
