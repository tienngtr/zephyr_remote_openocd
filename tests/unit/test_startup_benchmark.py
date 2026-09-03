# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "startup_benchmark", ROOT / "tests/startup_benchmark.py"
)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class StartupBenchmarkTests(unittest.TestCase):
    def test_percentile_interpolates_inclusive_rank(self):
        self.assertEqual(benchmark.percentile([1.0, 2.0, 4.0, 8.0], 0), 1.0)
        self.assertEqual(benchmark.percentile([1.0, 2.0, 4.0, 8.0], 1), 8.0)
        self.assertAlmostEqual(benchmark.percentile([1.0, 2.0, 4.0, 8.0], 0.95), 7.4)

    def test_summary_contains_first_median_p95_worst_and_count(self):
        result = benchmark.summarize([0.1, 0.2, 0.3, 0.4])
        self.assertEqual(result["first"], 0.1)
        self.assertEqual(result["median"], 0.25)
        self.assertEqual(result["worst"], 0.4)
        self.assertEqual(result["iterations"], 4)

    def test_overhead_is_paired_remote_minus_baseline(self):
        result = benchmark.overhead_summary([1.0, 2.0], [1.5, 2.25])
        self.assertEqual(result["first"], 0.5)
        self.assertEqual(result["median"], 0.375)

    def test_empty_and_mismatched_samples_are_rejected(self):
        with self.assertRaises(ValueError):
            benchmark.summarize([])
        with self.assertRaises(ValueError):
            benchmark.overhead_summary([1.0], [])

    def test_threshold_uses_median_and_strictly_less_than(self):
        self.assertTrue(benchmark.passes_threshold([0.1, 0.2, 0.3], 0.5))
        self.assertFalse(benchmark.passes_threshold([0.5, 0.5, 0.5], 0.5))
        with self.assertRaises(ValueError):
            benchmark.passes_threshold([0.1], 0)

    def test_json_result_has_machine_readable_schema_and_pass_logic(self):
        result = {
            "benchmark_version": benchmark.BENCHMARK_VERSION,
            "schema": "zro.startup-overhead.v1",
            "overhead": {"pass": True, "threshold_seconds": 0.5},
        }
        encoded = json.dumps(result)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["schema"], "zro.startup-overhead.v1")
        self.assertTrue(decoded["overhead"]["pass"])

    def test_optional_environment_metadata_is_unknown_when_unavailable(self):
        args = SimpleNamespace(
            warmup=5,
            iterations=100,
            command="flash",
            build_dir=Path("build"),
            config=Path("config.toml"),
        )
        with patch.object(benchmark.subprocess, "run", side_effect=OSError):
            metadata = benchmark._metadata(ROOT, args)
        self.assertEqual(metadata["revision"], "unknown")
        self.assertEqual(metadata["cpu"], "unknown")


if __name__ == "__main__":
    unittest.main()
