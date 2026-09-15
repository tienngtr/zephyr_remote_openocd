# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import sys

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location("local_report", ROOT / "scripts/local_report.py")
assert SPEC is not None and SPEC.loader is not None
local_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = local_report
SPEC.loader.exec_module(local_report)


def test_default_commands_cover_tests_coverage_and_working_tree(monkeypatch):
    monkeypatch.setattr(local_report.sys, "executable", "/python")

    commands = local_report.commands(local_report.parse_args(["--base-revision", "main"]))

    assert commands == [
        (
            (
                "/python",
                "-m",
                "pytest",
                "--cov",
                "--cov-config=.coveragerc",
                "--cov-report=",
            ),
            None,
        ),
        (("/python", "scripts/coverage_summary.py"), None),
        (
            (
                "/python",
                "scripts/radon_summary.py",
                "--working-tree",
                "--base-revision",
                "main",
            ),
            None,
        ),
    ]


def test_zephyr_base_adds_adapter_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(local_report.sys, "executable", "/python")

    commands = local_report.commands(local_report.parse_args(["--zephyr-base", str(tmp_path)]))

    command, environment = commands[1]
    assert command[:5] == (
        "/python",
        "-m",
        "pytest",
        "tests/zephyr_integration/test_adapter.py",
        "-m",
    )
    assert "--cov-append" in command
    assert environment is not None
    assert environment["ZEPHYR_BASE"] == str(tmp_path.resolve())
