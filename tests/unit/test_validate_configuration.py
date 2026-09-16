# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from zephyr_remote_openocd.config import ResolvedRemote
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "validate_configuration", ROOT / "scripts/validate_configuration.py"
)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def test_explicit_remote_summary_includes_effective_non_secret_settings(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "default_runner: remote_openocd\n"
        "presets:\n"
        "  controlled_preset:\n"
        "    ssh_command: [ssh, -F, /controlled/ssh-config]\n"
        "remotes:\n"
        "  controlled_remote:\n"
        "    preset: controlled_preset\n"
        "    ssh_host: controlled-host\n"
        "    openocd_command: [/controlled/openocd, --verbose]\n"
        "    forward_env: [CONTROLLED_TOKEN]\n"
        "    path_mappings: {/controlled/local: /controlled/remote}\n",
        encoding="utf-8",
    )

    assert validator.main([str(path), "--remote", "controlled_remote"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    for value in (
        str(path),
        "remote_openocd",
        "controlled_preset",
        "controlled_remote",
        "controlled-host",
        "ssh",
        "-F",
        "/controlled/ssh-config",
        "/controlled/openocd",
        "--verbose",
        "CONTROLLED_TOKEN",
        "/controlled/local",
        "/controlled/remote",
    ):
        assert value in output.out


def test_default_remote_is_resolved_without_environment_override(
    tmp_path: Path, monkeypatch
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
    remotes: list[ResolvedRemote] = []
    monkeypatch.setattr(validator, "_print_remote", remotes.append)
    assert validator.main([str(path)]) == 0
    assert [remote.name for remote in remotes] == ["configured"]
    assert remotes[0].openocd_command == ("openocd",)


def test_no_selected_remote_reports_structural_success(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("remotes:\n  incomplete: {}\n")
    monkeypatch.setattr(
        validator,
        "resolve_remote",
        lambda *_args, **_kwargs: pytest.fail("an unselected remote must not be resolved"),
    )
    assert validator.main([str(path)]) == 0


def test_default_path_honors_configuration_environment(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text("{}\n")
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(path))
    loaded: list[Path] = []
    load_config = validator.load_config

    def capture_path(candidate):
        loaded.append(candidate)
        return load_config(candidate)

    monkeypatch.setattr(validator, "load_config", capture_path)
    assert validator.main([]) == 0
    assert loaded == [path]


def test_missing_file_fails_with_setup_guidance(tmp_path: Path, capsys) -> None:
    path = tmp_path / "missing.yaml"
    assert validator.main([str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert str(path) in output.err
    assert "python3 scripts/setup.py" in output.err
    assert "provide CONFIG" in output.err


def test_inspection_failure_is_not_reported_as_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "config.yaml"

    def inspection_failure(_path: Path) -> None:
        raise PermissionError("permission denied")

    def load_failure(config_path: Path):
        raise validator.ConfigError(f"cannot read configuration {config_path}: permission denied")

    monkeypatch.setattr(validator.Path, "lstat", inspection_failure)
    monkeypatch.setattr(validator, "load_config", load_failure)
    assert validator.main([str(path)]) == 1
    output = capsys.readouterr()
    assert "cannot read configuration" in output.err
    assert "permission denied" in output.err
    assert "does not exist" not in output.err
    assert "scripts/setup.py" not in output.err


def test_configuration_and_resolution_errors_are_actionable(tmp_path: Path, capsys) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("unknown: true\n")
    assert validator.main([str(invalid)]) == 1
    assert "invalid configuration" in capsys.readouterr().err

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


def test_validation_performs_no_external_io(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "default_remote: lab\nremotes:\n  lab:\n    openocd_command: [openocd]\n",
        encoding="utf-8",
    )

    guards = []
    for owner, name in (
        (subprocess, "run"),
        (subprocess, "Popen"),
        (os, "system"),
        (socket, "create_connection"),
        (socket, "socket"),
        (SshCommand, "run"),
        (SshCommand, "popen"),
        (SshCommand, "run_stream"),
    ):
        guard = Mock(side_effect=AssertionError(f"external I/O is forbidden: {name}"))
        monkeypatch.setattr(owner, name, guard)
        guards.append(guard)

    assert validator.main([str(path)]) == 0
    for guard in guards:
        guard.assert_not_called()
