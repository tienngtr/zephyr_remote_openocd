# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.support import ROOT


def _configure_module(
    tmp_path: Path, override: str, *, home_name: str = "home"
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
    )
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "ZEPHYR_REMOTE_OPENOCD_CONFIG": override,
        }
    )
    result = subprocess.run(
        ["cmake", "-S", str(source), "-B", str(build)],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    return result, build / "zro-result.txt"


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
