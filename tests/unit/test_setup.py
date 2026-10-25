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
from types import ModuleType

import pytest
from zephyr_remote_openocd.config import load_config

from tests.support import ROOT

SETUP = ROOT / "scripts" / "user" / "setup.py"
CONFIG_DEFAULT = ROOT / "zephyr" / "config_default.py"
TEMPLATE = ROOT / "resources" / "config.example.yaml"


def run_setup(
    home: Path,
    *arguments: str,
    cwd: Path | None = None,
    environment_override: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.update(environment_override or {})
    return subprocess.run(
        [sys.executable, str(SETUP), *arguments],
        cwd=cwd or home,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_creates_template_and_reports_activation(tmp_path: Path) -> None:
    result = run_setup(tmp_path, cwd=tmp_path.parent)
    config = tmp_path / ".config" / "zephyr_remote_openocd" / "config.yaml"
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == TEMPLATE.read_bytes()
    assert str(config) in result.stdout
    assert str(ROOT) in result.stdout
    assert "EXTRA_ZEPHYR_MODULES" in result.stdout
    validator = ROOT / "scripts" / "user" / "validate_configuration.py"
    assert str(validator) in result.stdout
    assert "--remote" in result.stdout
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_creates_template_at_explicit_path_without_chmodding_parent(tmp_path: Path) -> None:
    parent = tmp_path / "configs"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    config = parent / "lab.yaml"
    result = run_setup(tmp_path, str(config))
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == TEMPLATE.read_bytes()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert not (tmp_path / ".config").exists()
    validator = ROOT / "scripts" / "user" / "validate_configuration.py"
    assert f"python3 {validator} {config} --remote NAME" in result.stdout
    assert f"export ZEPHYR_REMOTE_OPENOCD_CONFIG={config}" in result.stdout


def test_explicit_path_requires_existing_parent(tmp_path: Path) -> None:
    config = tmp_path / "missing" / "config.yaml"
    result = run_setup(tmp_path, str(config))
    assert result.returncode != 0
    assert "does not exist; create it first" in result.stderr
    assert not config.parent.exists()


@pytest.mark.parametrize("spelling", ("relative", "absolute", "symlink-parent"))
@pytest.mark.parametrize("existing", (False, True), ids=("created", "reused"))
def test_parent_path_setup_exports_the_selected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str, existing: bool
) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    target = tmp_path / "target"
    (target / "inner").mkdir(parents=True)
    config = target / "config with spaces.yaml"
    contents = "default_runner: remote_openocd\n" if existing else TEMPLATE.read_text()
    if existing:
        config.write_text(contents)
        config.chmod(0o644)
    if spelling == "symlink-parent":
        (caller / "link").symlink_to(target / "inner", target_is_directory=True)
        argument = f"link/../{config.name}"
    elif spelling == "absolute":
        argument = f"{caller}/../target/{config.name}"
    else:
        argument = f"../target/{config.name}"
    decoy = caller / config.name
    decoy.write_text("unrelated configuration\n")

    result = run_setup(tmp_path, argument, cwd=caller)

    assert result.returncode == 0, result.stderr
    assert config.read_text() == contents
    assert decoy.read_text() == "unrelated configuration\n"
    assert stat.S_IMODE(config.stat().st_mode) == (0o644 if existing else 0o600)
    exported = _exported_configuration(result.stdout)
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", exported)
    document = load_config()
    assert Path(document.path).samefile(config)
    assert document.default_runner == ("remote_openocd" if existing else "openocd")


def test_parent_path_export_preserves_final_configuration_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    directory = tmp_path / "configs"
    directory.mkdir()
    config = directory / "actual.yaml"
    config.write_text("default_runner: remote_openocd\n")
    link = directory / "selected.yaml"
    link.symlink_to(config)

    result = run_setup(tmp_path, "../configs/selected.yaml", cwd=caller)

    assert result.returncode == 0, result.stderr
    exported = _exported_configuration(result.stdout)
    assert Path(exported).is_symlink()
    assert Path(exported).samefile(link)
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", exported)
    assert load_config().default_runner == "remote_openocd"


def _exported_configuration(output: str) -> str:
    command = next(
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith("export ZEPHYR_REMOTE_OPENOCD_CONFIG=")
    )
    result = subprocess.run(
        ["bash", "-c", 'set -e\n' + command + '\nprintf "%s" "$ZEPHYR_REMOTE_OPENOCD_CONFIG"'],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_explicit_default_path_uses_default_rules(tmp_path: Path) -> None:
    config = tmp_path / ".config" / "zephyr_remote_openocd" / "config.yaml"
    result = run_setup(tmp_path, str(config))
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert "ZEPHYR_REMOTE_OPENOCD_CONFIG" not in result.stdout


def test_setup_ignores_configuration_path_override(tmp_path: Path) -> None:
    override = tmp_path / "custom" / "config.yaml"
    result = run_setup(
        tmp_path, environment_override={"ZEPHYR_REMOTE_OPENOCD_CONFIG": str(override)}
    )
    config = tmp_path / ".config" / "zephyr_remote_openocd" / "config.yaml"
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == TEMPLATE.read_bytes()
    assert not override.parent.exists()


def test_existing_configuration_is_preserved_and_not_chmodded(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "zephyr_remote_openocd"
    config_dir.mkdir(parents=True)
    config_parent = tmp_path / ".config"
    config = config_dir / "config.yaml"
    config.write_text('default_runner: remote_openocd\n')
    config_parent.chmod(0o755)
    config_dir.chmod(0o755)
    config.chmod(0o644)
    result = run_setup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert config.read_text() == 'default_runner: remote_openocd\n'
    assert stat.S_IMODE(config_parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


def test_existing_non_file_configuration_fails_actionably(tmp_path: Path) -> None:
    config = tmp_path / ".config" / "zephyr_remote_openocd" / "config.yaml"
    config.mkdir(parents=True)
    result = run_setup(tmp_path)
    assert result.returncode != 0
    assert "not a regular file" in result.stderr


def test_initialize_config_preserves_file_created_after_absence_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = load_setup_module()
    destination = tmp_path / ".config" / "zephyr_remote_openocd" / "config.yaml"
    concurrent_contents = b"created by another setup process\n"
    original_open = os.open

    def create_concurrent_file_then_raise(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if Path(path) == destination:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            try:
                os.write(descriptor, concurrent_contents)
            finally:
                os.close(descriptor)
            raise FileExistsError(path)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(setup.os, "open", create_concurrent_file_then_raise)

    created = setup.initialize_config(ROOT, destination)

    assert created is False
    assert destination.read_bytes() == concurrent_contents


def load_setup_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("zro_setup", SETUP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_config_default_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("zro_config_default", CONFIG_DEFAULT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_default_finds_module_by_markers(tmp_path: Path) -> None:
    module = load_config_default_module()
    root = tmp_path / "module"
    nested = root / "changed" / "layout"
    (root / "zephyr").mkdir(parents=True)
    (root / "zephyr" / "module.yml").write_text("name: example\n")
    (root / "python" / "zephyr_remote_openocd").mkdir(parents=True)
    (root / "python" / "zephyr_remote_openocd" / "__init__.py").write_text("")
    nested.mkdir(parents=True)

    assert module.find_module_root(nested) == root
    with pytest.raises(RuntimeError):
        module.find_module_root(tmp_path / "missing")


def test_config_default_reports_invalid_configuration_to_cmake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "invalid.yaml"
    config.write_text("unknown: true\n", encoding="utf-8")

    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    result = subprocess.run(
        [sys.executable, str(CONFIG_DEFAULT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "invalid configuration" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("missing_module", "missing_name"),
    (("elftools", "pyelftools"), ("yaml", "PyYAML"), ("jsonschema", "jsonschema")),
)
def test_missing_dependency_warns_without_blocking_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_module: str, missing_name: str
) -> None:
    setup = load_setup_module()
    destination = tmp_path / ".config" / "zephyr_remote_openocd" / "config.yaml"
    print_dependency_status = setup._print_dependency_status
    monkeypatch.setattr(setup, "module_root", lambda: ROOT)
    monkeypatch.setattr(setup, "config_path", lambda: destination)
    monkeypatch.setattr(
        setup,
        "_print_dependency_status",
        lambda: print_dependency_status(lambda name: None if name == missing_module else object()),
    )

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert setup.main([]) == 0

    text = output.getvalue()
    assert destination.is_file()
    assert f"{missing_name}: missing" in text
    for dependency_name in ("pyelftools", "PyYAML", "jsonschema"):
        if dependency_name != missing_name:
            assert f"{dependency_name}: found" in text
    assert "Zephyr 4.4" in text
    assert "pip install" not in text
    assert "requirements.txt" not in text


def test_activation_command_preserves_module_path_with_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = load_setup_module()
    root = tmp_path / "module root"
    destination = tmp_path / "user home" / ".config/zro/config.yaml"
    monkeypatch.setattr(setup, "module_root", lambda: root)
    monkeypatch.setattr(setup, "config_path", lambda: destination)
    monkeypatch.setattr(setup, "initialize_config", lambda *_, **__: True)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert setup.main([]) == 0
    activation = next(
        line.strip()
        for line in output.getvalue().splitlines()
        if line.strip().startswith("export ")
    )
    result = subprocess.run(
        ["bash", "-c", activation + '\nprintf "%s" "$EXTRA_ZEPHYR_MODULES"'],
        env={**os.environ, "EXTRA_ZEPHYR_MODULES": "/existing module"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == f"/existing module;{root}"
