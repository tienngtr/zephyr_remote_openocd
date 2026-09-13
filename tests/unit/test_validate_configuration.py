# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "validate_configuration", ROOT / "scripts/validate_configuration.py"
)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)

EXAMPLE = ROOT / "resources/config.example.yaml"


def test_example_resolves_explicit_remote(capsys) -> None:
    assert validator.main([str(EXAMPLE), "--remote", "lab"]) == 0
    output = capsys.readouterr()
    assert f"Configuration valid: {EXAMPLE}" in output.out
    assert "Default runner: openocd" in output.out
    assert "Presets: (none)" in output.out
    assert "Remotes: lab" in output.out
    assert "Resolved remote: lab" in output.out
    assert 'SSH command: ["ssh"]' in output.out
    assert (
        'OpenOCD command: ["/opt/zephyr-sdk-1.0.1/hosttools/sysroots/'
        'x86_64-pokysdk-linux/usr/bin/openocd"]' in output.out
    )
    assert "Forwarded environment names: (none)" in output.out
    assert "Path mappings: (none)" in output.out
    assert output.err == ""


def test_default_remote_is_resolved_without_environment_override(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "default_remote: configured\n"
        "remotes:\n"
        "  configured:\n"
        "    openocd_command: [openocd]\n"
        "  environment:\n"
        "    openocd_command: [other-openocd]\n"
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "environment")
    assert validator.main([str(path)]) == 0
    output = capsys.readouterr()
    assert "Resolved remote: configured" in output.out
    assert "other-openocd" not in output.out


def test_no_selected_remote_reports_structural_success(tmp_path: Path, capsys) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("remotes:\n  incomplete: {}\n")
    assert validator.main([str(path)]) == 0
    output = capsys.readouterr()
    assert "Configuration valid:" in output.out
    assert "Remotes: incomplete" in output.out
    assert "No default remote is configured." in output.out
    assert "python3 scripts/validate_configuration.py [CONFIG] --remote NAME" in output.out


def test_default_path_honors_configuration_environment(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text("{}\n")
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(path))
    assert validator.main([]) == 0
    assert f"Configuration valid: {path}" in capsys.readouterr().out


def test_missing_file_fails_with_setup_guidance(tmp_path: Path, capsys) -> None:
    path = tmp_path / "missing.yaml"
    assert validator.main([str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert str(path) in output.err
    assert "python3 scripts/setup.py" in output.err
    assert "provide CONFIG" in output.err


def test_configuration_and_resolution_errors_are_actionable(tmp_path: Path, capsys) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("unknown: true\n")
    assert validator.main([str(invalid)]) == 1
    assert "Configuration invalid: invalid configuration" in capsys.readouterr().err

    incomplete = tmp_path / "incomplete.yaml"
    incomplete.write_text("remotes:\n  lab: {}\n")
    assert validator.main([str(incomplete), "--remote", "lab"]) == 1
    output = capsys.readouterr()
    assert "openocd_command is required for remote 'lab'" in output.err


def test_summary_does_not_read_or_print_forwarded_values(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "remotes:\n  lab:\n    openocd_command: [openocd]\n    forward_env: [PRIVATE_VALUE]\n"
    )
    monkeypatch.setenv("PRIVATE_VALUE", "do-not-print-this")
    assert validator.main([str(path), "--remote", "lab"]) == 0
    output = capsys.readouterr().out
    assert "PRIVATE_VALUE" in output
    assert "do-not-print-this" not in output


def test_validation_performs_no_external_io(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("external I/O is forbidden")

    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert validator.main([str(EXAMPLE), "--remote", "lab"]) == 0
