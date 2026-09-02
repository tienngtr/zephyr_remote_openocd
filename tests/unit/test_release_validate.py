# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "release_validate", ROOT / "scripts/release_validate.py"
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


def arguments(**overrides):
    values = {
        "zephyr_base": Path("/zephyr"),
        "west": Path("/west"),
        "board": "native_sim/native/64",
        "hardware_config": Path("/fixtures/hardware.toml"),
        "benchmark_build_dir": Path("/build"),
        "benchmark_config": Path("/config.yaml"),
        "benchmark_cwd": Path("/workspace"),
        "benchmark_command": "flash",
        "benchmark_warmup": 5,
        "benchmark_iterations": 100,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_steps_has_stable_order_and_external_layers():
    steps = release.build_steps(arguments())
    assert [step.name for step in steps] == [
        "pytest",
        "static_check",
        "zephyr",
        "ssh",
        "hardware",
        "benchmark",
    ]
    assert "--hardware-config" in steps[-2].command


def test_build_steps_requires_external_evidence_inputs():
    with pytest.raises(ValueError, match="hardware-config"):
        release.build_steps(arguments(hardware_config=None))
    with pytest.raises(ValueError, match="zephyr-base"):
        release.build_steps(arguments(zephyr_base=None))
    with pytest.raises(ValueError, match="benchmark-build-dir"):
        release.build_steps(
            arguments(benchmark_build_dir=None, benchmark_config=None, benchmark_cwd=None)
        )


def test_inventory_capability_gate_reports_missing_evidence(tmp_path):
    class Profile:
        capabilities = ("flash",)

    class Target:
        profiles = (Profile(),)

    with (
        patch("tests.inventory.load_inventory", return_value=SimpleNamespace(targets=(Target(),))),
        pytest.raises(ValueError, match="required capabilities"),
    ):
        release.validate_inventory_capabilities(tmp_path / "inventory.toml")


def test_inventory_capability_report_is_machine_readable():
    class Profile:
        capabilities = tuple(release.REQUIRED_CAPABILITIES)

    class Target:
        profiles = (Profile(),)

    with patch("tests.inventory.load_inventory", return_value=SimpleNamespace(targets=(Target(),))):
        report = release.inventory_capabilities(Path("inventory.toml"))
    assert report["pass"] is True
    assert report["missing"] == []


def test_benchmark_result_extracts_json_from_combined_output():
    assert release.benchmark_result('{"overhead": {"pass": true}}\nsummary') == {
        "overhead": {"pass": True}
    }
    assert release.benchmark_result("no benchmark output") is None


def test_run_steps_stops_after_first_failure_and_preserves_order():
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = f"out-{returncode}"
            self.stderr = ""

    def executor(command, **kwargs):
        calls.append((command, kwargs))
        return Result(1 if len(calls) == 2 else 0)

    results = release.run_steps(
        [
            release.Step("first", ("one",)),
            release.Step("second", ("two",)),
            release.Step("third", ("three",)),
        ],
        executor,
    )
    assert [item.name for item in results] == ["first", "second"]
    assert len(calls) == 2
    assert calls[0][1]["check"] is False


def test_remote_leak_scan_pattern_cannot_match_its_own_command(tmp_path):
    host = SimpleNamespace(id="lab", address="host", ssh_command=("ssh",))
    completed = SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch(
            "tests.inventory.load_inventory",
            return_value=SimpleNamespace(hosts=(host,)),
        ),
        patch.object(release.subprocess, "run", return_value=completed) as run,
    ):
        report = release.remote_leak_scan(tmp_path / "inventory.toml")

    remote_command = run.call_args.args[0][-1]
    assert re.search(release.REMOTE_LEAK_PATTERN, remote_command) is None
    for process in (
        "python -m zephyr_remote_openocd.remote_helper",
        "python remote_helper.py",
        "python helper.py",
        "/opt/openocd/bin/openocd -f board.cfg",
    ):
        assert re.search(release.REMOTE_LEAK_PATTERN, process)
    assert report["clean"] is True


def test_summary_contract_keeps_wsl_gates_deferred():
    assert release.DEFERRED_GATES == ("PG-012", "PG-013")


def test_strict_external_collection_rejects_skips(monkeypatch):
    class Reporter:
        stats = {"skipped": [object()]}

    class PluginManager:
        @staticmethod
        def get_plugin(name):
            assert name == "terminalreporter"
            return Reporter()

    session = SimpleNamespace(
        config=SimpleNamespace(pluginmanager=PluginManager()),
        exitstatus=0,
    )
    monkeypatch.setenv("ZRO_STRICT_EXTERNAL", "1")
    # The hook lives in the test conftest because pytest owns the result state.
    import tests.conftest as conftest

    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
