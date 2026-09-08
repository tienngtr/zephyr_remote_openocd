# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "startup_benchmark", ROOT / "tests/startup_benchmark.py"
)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_percentile_interpolates_inclusive_rank():
    assert benchmark.percentile([1.0, 2.0, 4.0, 8.0], 0) == 1.0
    assert benchmark.percentile([1.0, 2.0, 4.0, 8.0], 1) == 8.0
    assert benchmark.percentile([1.0, 2.0, 4.0, 8.0], 0.95) == pytest.approx(7.4)


def test_summary_contains_first_median_p95_worst_and_count():
    result = benchmark.summarize([0.1, 0.2, 0.3, 0.4])
    assert result["first"] == 0.1
    assert result["median"] == 0.25
    assert result["worst"] == 0.4
    assert result["iterations"] == 4


def test_overhead_is_paired_remote_minus_baseline():
    result = benchmark.overhead_summary([1.0, 2.0], [1.5, 2.25])
    assert result["first"] == 0.5
    assert result["median"] == 0.375


def test_empty_and_mismatched_samples_are_rejected():
    with pytest.raises(ValueError):
        benchmark.summarize([])
    with pytest.raises(ValueError):
        benchmark.overhead_summary([1.0], [])


def test_threshold_uses_median_and_strictly_less_than():
    assert benchmark.passes_threshold([0.1, 0.2, 0.3], 0.5)
    assert not benchmark.passes_threshold([0.5, 0.5, 0.5], 0.5)
    with pytest.raises(ValueError):
        benchmark.passes_threshold([0.1], 0)


@pytest.mark.parametrize(
    ("remote", "median", "status"),
    (([1.25, 2.125, 3.375], 0.25, 0), ([1.5, 2.5, 3.5], 0.5, 1)),
)
def test_benchmark_cli_reports_measured_overhead(monkeypatch, capsys, remote, median, status):
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark",
            "--build-dir",
            "build",
            "--config",
            "config.yaml",
            "--cwd",
            ".",
            "--warmup",
            "1",
            "--iterations",
            "3",
        ],
    )
    with (
        patch.object(benchmark, "_measure", side_effect=([1.0, 2.0, 3.0], remote)) as measure,
        patch.object(benchmark, "_metadata", return_value={}),
    ):
        assert benchmark.main() == status
    captured = capsys.readouterr()
    decoded = json.loads(captured.out)
    assert decoded["schema"] == "zro.startup-overhead"
    assert decoded["baseline"]["statistics"]["median"] == 2.0
    assert decoded["remote"]["statistics"]["iterations"] == 3
    assert decoded["overhead"]["statistics"]["median"] == median
    assert decoded["overhead"]["threshold_seconds"] == 0.5
    assert decoded["overhead"]["pass"] is (status == 0)
    assert "median additional startup" in captured.err
    baseline_call, remote_call = measure.call_args_list
    assert baseline_call.args[0][-3:] == ["-r", "openocd", "--context"]
    assert remote_call.args[0][-2:] == ["-r", "remote_openocd"]
    assert remote_call.args[1]["ZEPHYR_REMOTE_OPENOCD_RECORD"] == "1"
    assert remote_call.args[3:] == (1, 3)


def test_optional_environment_metadata_is_unknown_when_unavailable():
    args = SimpleNamespace(
        warmup=5,
        iterations=100,
        command="flash",
        build_dir=Path("build"),
        config=Path("config.yaml"),
    )
    with (
        patch.object(benchmark.subprocess, "run", side_effect=OSError),
        patch.object(benchmark.platform, "processor", side_effect=OSError),
    ):
        metadata = benchmark._metadata(ROOT, args)
    assert metadata["revision"] == "unknown"
    assert metadata["cpu"] == "unknown"
