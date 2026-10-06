# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.support import ROOT


def _configure_module(
    tmp_path: Path,
    override: str,
    *,
    generator: str | None = None,
    home_name: str = "home",
    suppress_regeneration: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    home = tmp_path / home_name
    source = tmp_path / "source"
    build = tmp_path / "build"
    home.mkdir(exist_ok=True)
    source.mkdir()
    module_cmake = (ROOT / "zephyr" / "CMakeLists.txt").as_posix()
    module_directory = (ROOT / "zephyr").as_posix()
    python = Path(sys.executable).as_posix()
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(zro_configuration NONE)\n"
        "function(load_zro_module)\n"
        f'  set(PYTHON_EXECUTABLE "{python}")\n'
        f'  set(ZEPHYR_CURRENT_CMAKE_DIR "{module_directory}")\n'
        "  set_property(GLOBAL PROPERTY ZEPHYR_RUNNERS openocd)\n"
        f'  include("{module_cmake}")\n'
        "  get_property(zro_dependencies DIRECTORY PROPERTY CMAKE_CONFIGURE_DEPENDS)\n"
        '  set(ZRO_DEPENDENCIES "${zro_dependencies}" PARENT_SCOPE)\n'
        "endfunction()\n"
        "load_zro_module()\n"
        'file(WRITE "${CMAKE_BINARY_DIR}/zro-result.txt" '
        '"${ZRO_DEPENDENCIES}\\n${BOARD_FLASH_RUNNER}\\n")\n'
        'file(APPEND "${CMAKE_BINARY_DIR}/configuration-runs.txt" "configured\\n")\n'
        'file(WRITE "${PROJECT_BINARY_DIR}/runners.yaml" '
        '"flash-runner: ${BOARD_FLASH_RUNNER}\\n'
        'debug-runner: ${BOARD_DEBUG_RUNNER}\\n'
        'runners: [openocd, remote_openocd]\\n'
        'args: {openocd: [--verify], remote_openocd: [--verify]}\\n'
        'config: {elf_file: firmware.elf}\\n")\n'
    )
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "ZEPHYR_REMOTE_OPENOCD_CONFIG": override,
        }
    )
    command = ["cmake", "-S", str(source), "-B", str(build)]
    if generator is not None:
        command.extend(("-G", generator))
    if suppress_regeneration:
        command.append("-DCMAKE_SUPPRESS_REGENERATION=ON")
    result = subprocess.run(
        command,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    return result, build / "zro-result.txt"


def _build_module(
    build: Path, *, home: Path, override: str, target: str | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "ZEPHYR_REMOTE_OPENOCD_CONFIG": override,
        }
    )
    command = ["cmake", "--build", str(build)]
    if target is not None:
        command.extend(("--target", target))
    return subprocess.run(
        command,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )


def test_cmake_empty_config_override_uses_default_path(tmp_path: Path):
    default = tmp_path / "home" / ".config" / "zephyr_remote_openocd" / "config.yaml"
    default.parent.mkdir(parents=True)
    default.write_text("default_runner: remote_openocd\n")

    result, report = _configure_module(tmp_path, "")

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == [str(default), "remote_openocd"]


def test_cmake_tilde_config_override_tracks_expanded_path(tmp_path: Path):
    selected = tmp_path / "home" / "custom.yaml"
    selected.parent.mkdir(parents=True)
    selected.write_text("default_runner: remote_openocd\n")

    result, report = _configure_module(tmp_path, "~/custom.yaml")

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == [str(selected), "remote_openocd"]


def test_cmake_tilde_expansion_treats_home_as_literal(tmp_path: Path):
    selected = tmp_path / "home\\1" / "custom.yaml"
    selected.parent.mkdir(parents=True)
    selected.write_text("default_runner: remote_openocd\n")

    result, report = _configure_module(tmp_path, "~/custom.yaml", home_name="home\\1")

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == [str(selected), "remote_openocd"]


@pytest.mark.parametrize("generator", ("Ninja", "Unix Makefiles"))
@pytest.mark.parametrize(
    "override_name",
    (None, "custom[*?].yaml"),
    ids=("default-location", "override-location"),
)
def test_cmake_config_presence_changes_regenerate_default_runner(
    tmp_path: Path, generator: str, override_name: str | None
):
    home = tmp_path / "home"
    if override_name is None:
        config = home / ".config" / "zephyr_remote_openocd" / "config.yaml"
        override = ""
    else:
        config = tmp_path / override_name
        override = str(config)

    result, report = _configure_module(tmp_path, override, generator=generator)

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == [str(config), "openocd"]

    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("default_runner: remote_openocd\n")
    result = _build_module(report.parent, home=home, override=override)

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == [str(config), "remote_openocd"]

    config.unlink()
    result = _build_module(report.parent, home=home, override=override)

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == [str(config), "openocd"]


@pytest.mark.parametrize("target", (None, "help"), ids=("default-target", "help-target"))
def test_ninja_config_path_switch_regenerates_default_runner(tmp_path: Path, target: str | None):
    home = tmp_path / "home"
    default = home / ".config" / "zephyr_remote_openocd" / "config.yaml"
    default.parent.mkdir(parents=True)
    default.write_text("default_runner: openocd\n")
    selected = tmp_path / "selected config[*?].yaml"
    selected.write_text("default_runner: remote_openocd\n")

    result, report = _configure_module(tmp_path, "", generator="Ninja")
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines()[-1] == "openocd"

    result = _build_module(report.parent, home=home, override=str(selected), target=target)
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines()[-1] == "remote_openocd"

    configurations = report.parent / "configuration-runs.txt"
    assert len(configurations.read_text().splitlines()) == 2
    result = _build_module(report.parent, home=home, override=str(selected), target=target)
    assert result.returncode == 0, result.stdout
    assert len(configurations.read_text().splitlines()) == 2

    result = _build_module(report.parent, home=home, override="", target=target)
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines()[-1] == "openocd"


def test_ninja_config_path_check_survives_clean(tmp_path: Path):
    result, report = _configure_module(tmp_path, "", generator="Ninja")
    assert result.returncode == 0, result.stdout
    home = tmp_path / "home"
    result = _build_module(report.parent, home=home, override="", target="clean")
    assert result.returncode == 0, result.stdout

    selected = tmp_path / "selected.yaml"
    selected.write_text("default_runner: remote_openocd\n")
    result = _build_module(report.parent, home=home, override=str(selected))
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines()[-1] == "remote_openocd"


def test_ninja_config_path_switch_rejects_invalid_selected_config(tmp_path: Path):
    result, report = _configure_module(tmp_path, "", generator="Ninja")
    assert result.returncode == 0, result.stdout
    home = tmp_path / "home"
    selected = tmp_path / "invalid.yaml"
    selected.write_text("default_runner: unsupported\n")

    result = _build_module(report.parent, home=home, override=str(selected))
    assert result.returncode != 0
    assert report.read_text().splitlines()[-1] == "openocd"

    selected.write_text("default_runner: remote_openocd\n")
    result = _build_module(report.parent, home=home, override=str(selected))
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines()[-1] == "remote_openocd"


def test_ninja_suppressed_regeneration_keeps_existing_default_runner(tmp_path: Path):
    result, report = _configure_module(tmp_path, "", generator="Ninja", suppress_regeneration=True)
    assert result.returncode == 0, result.stdout
    selected = tmp_path / "selected.yaml"
    selected.write_text("default_runner: remote_openocd\n")

    result = _build_module(report.parent, home=tmp_path / "home", override=str(selected))

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines()[-1] == "openocd"


@pytest.mark.parametrize("target", (None, "zro_runner_state"), ids=("default", "metadata"))
def test_make_config_path_switch_refreshes_runner_metadata(tmp_path: Path, target: str | None):
    selected = tmp_path / "selected config[*?].yaml"
    selected.write_text("default_runner: remote_openocd\n")
    result, report = _configure_module(tmp_path, "", generator="Unix Makefiles")
    assert result.returncode == 0, result.stdout
    metadata = report.parent / "runners.yaml"
    before = yaml.safe_load(metadata.read_text())
    home = tmp_path / "home"

    result = _build_module(report.parent, home=home, override=str(selected), target=target)
    assert result.returncode == 0, result.stdout
    state = yaml.safe_load(metadata.read_text())
    assert state == {**before, "flash-runner": "remote_openocd", "debug-runner": "remote_openocd"}
    # Updating metadata must not reconfigure the active Make build graph.
    assert len((report.parent / "configuration-runs.txt").read_text().splitlines()) == 1
    modified = metadata.stat().st_mtime_ns
    result = _build_module(report.parent, home=home, override=str(selected), target=target)
    assert result.returncode == 0, result.stdout
    assert metadata.stat().st_mtime_ns == modified

    result = _build_module(report.parent, home=home, override="", target=target)
    assert result.returncode == 0, result.stdout
    assert yaml.safe_load(metadata.read_text()) == before


def test_make_config_path_switch_rejects_invalid_config_and_recovers(tmp_path: Path):
    selected = tmp_path / "selected.yaml"
    selected.write_text("default_runner: unsupported\n")
    result, report = _configure_module(tmp_path, "", generator="Unix Makefiles")
    assert result.returncode == 0, result.stdout
    metadata = report.parent / "runners.yaml"
    before = metadata.read_bytes()
    home = tmp_path / "home"

    result = _build_module(report.parent, home=home, override=str(selected))
    assert result.returncode != 0
    assert metadata.read_bytes() == before

    selected.write_text("default_runner: remote_openocd\n")
    result = _build_module(report.parent, home=home, override=str(selected))
    assert result.returncode == 0, result.stdout
    assert yaml.safe_load(metadata.read_text())["flash-runner"] == "remote_openocd"


def test_make_config_path_refresh_survives_clean(tmp_path: Path):
    result, report = _configure_module(tmp_path, "", generator="Unix Makefiles")
    assert result.returncode == 0, result.stdout
    home = tmp_path / "home"
    result = _build_module(report.parent, home=home, override="", target="clean")
    assert result.returncode == 0, result.stdout
    selected = tmp_path / "selected.yaml"
    selected.write_text("default_runner: remote_openocd\n")

    result = _build_module(report.parent, home=home, override=str(selected))

    assert result.returncode == 0, result.stdout
    assert (
        yaml.safe_load((report.parent / "runners.yaml").read_text())["debug-runner"]
        == "remote_openocd"
    )


def test_make_suppressed_regeneration_keeps_existing_default_runner(tmp_path: Path):
    result, report = _configure_module(
        tmp_path, "", generator="Unix Makefiles", suppress_regeneration=True
    )
    assert result.returncode == 0, result.stdout
    selected = tmp_path / "selected.yaml"
    selected.write_text("default_runner: remote_openocd\n")

    result = _build_module(report.parent, home=tmp_path / "home", override=str(selected))

    assert result.returncode == 0, result.stdout
    assert yaml.safe_load((report.parent / "runners.yaml").read_text())["flash-runner"] == "openocd"
