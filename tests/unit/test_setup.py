# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import stat
import subprocess
import sys
from pathlib import Path

from tests.support import ROOT

SETUP = ROOT / "scripts" / "setup.py"
TEMPLATE = ROOT / "resources" / "config.toml.example"


def run_setup(home: Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SETUP)],
        cwd=cwd or home,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_creates_template_and_reports_activation(tmp_path: Path):
    result = run_setup(tmp_path, tmp_path.parent)
    config = tmp_path / ".config" / "zephyr-remote-openocd" / "config.toml"
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == TEMPLATE.read_bytes()
    assert f"Configuration (created): {config}" in result.stdout
    assert f"Module root: {ROOT}" in result.stdout
    assert "EXTRA_ZEPHYR_MODULES" in result.stdout
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_existing_configuration_is_preserved_and_not_chmodded(tmp_path: Path):
    config_dir = tmp_path / ".config" / "zephyr-remote-openocd"
    config_dir.mkdir(parents=True)
    config_parent = tmp_path / ".config"
    config = config_dir / "config.toml"
    config.write_text('[zephyr]\ndefault = "remote"\n')
    config_parent.chmod(0o755)
    config_dir.chmod(0o755)
    config.chmod(0o644)
    result = run_setup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"Configuration (already exists): {config}" in result.stdout
    assert config.read_text() == '[zephyr]\ndefault = "remote"\n'
    assert stat.S_IMODE(config_parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


def test_existing_non_file_configuration_fails_actionably(tmp_path: Path):
    config = tmp_path / ".config" / "zephyr-remote-openocd" / "config.toml"
    config.mkdir(parents=True)
    result = run_setup(tmp_path)
    assert result.returncode != 0
    assert "not a regular file" in result.stderr


def load_setup_module():
    spec = importlib.util.spec_from_file_location("zro_setup", SETUP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dependency_detection_found_and_missing():
    setup = load_setup_module()
    assert setup.pyelftools_available(lambda _: object())
    assert not setup.pyelftools_available(lambda _: None)


def test_dependency_status_messages():
    setup = load_setup_module()
    found = io.StringIO()
    with contextlib.redirect_stdout(found):
        setup._print_dependency_status(lambda _: object())
    assert "pyelftools: found" in found.getvalue()

    missing = io.StringIO()
    with contextlib.redirect_stdout(missing):
        setup._print_dependency_status(lambda _: None)
    assert "Warning: pyelftools is not available" in missing.getvalue()
    assert "Use the Python environment configured for Zephyr" in missing.getvalue()
