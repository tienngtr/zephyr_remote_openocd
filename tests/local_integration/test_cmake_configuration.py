# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.config import ConfigError, load_config

from tests.support import ROOT


def _configure_module(
    tmp_path: Path,
    override: str,
    *,
    generator: str | None = None,
    home_name: str = "home",
    cwd: Path | None = None,
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
        "endfunction()\n"
        "load_zro_module()\n"
        'file(WRITE "${CMAKE_BINARY_DIR}/zro-result.txt" '
        '"${BOARD_FLASH_RUNNER}\\n")\n'
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
    result = subprocess.run(
        command,
        cwd=cwd,
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


@pytest.mark.parametrize(
    "text",
    (
        "default_runner: [\n",
        "default_runner: remote_openocd\ndefault_runner: openocd\n",
        "default_runner: openocd\nremotes:\n  lab:\n    ssh_host: a\n    ssh_host: b\n",
        "default_runnner: remote_openocd\n",
        "default_runner: openocd\nunknown: true\n",
        "default_runner: elsewhere\n",
        "default_runner: null\n",
        "default_runner: true\n",
        "default_runner: 1\n",
        "default_runner: [remote_openocd]\n",
        "default_runner: {runner: remote_openocd}\n",
        "default_runner: openocd\n---\ndefault_runner: remote_openocd\n",
        "null\n",
        "[]\n",
        "1: remote_openocd\n",
    ),
    ids=(
        "malformed-yaml",
        "duplicate-runner",
        "duplicate-remote-key",
        "misspelled-runner",
        "unknown-key",
        "invalid-runner",
        "null-runner",
        "bool-runner",
        "number-runner",
        "list-runner",
        "mapping-runner",
        "multiple-documents",
        "null-root",
        "list-root",
        "non-string-key",
    ),
)
def test_cmake_rejects_untrustworthy_runner_selection(tmp_path: Path, text: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(text)

    result, _ = _configure_module(tmp_path, str(config))

    assert result.returncode != 0
    assert str(config) in result.stdout
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize(
    "runner", ("openocd", "remote_openocd", None), ids=("local", "remote", "omitted")
)
@pytest.mark.parametrize(
    "remote_settings",
    (
        "default_remote: [invalid]\n",
        "presets: null\n",
        "remotes: false\n",
        "remotes:\n  lab:\n    invalid_remote_field: value\n",
        "remotes:\n  lab:\n    path_mappings:\n      /same: /one\n      /same/.: /two\n",
    ),
    ids=(
        "default-remote-type",
        "presets-type",
        "remotes-type",
        "remote-field",
        "mapping-collision",
    ),
)
def test_cmake_selects_runner_without_remote_validation(
    tmp_path: Path, runner: str | None, remote_settings: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text((f"default_runner: {runner}\n" if runner else "") + remote_settings)

    result, report = _configure_module(tmp_path, str(config))

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == [runner or "openocd"]
    with pytest.raises(ConfigError):
        load_config(config)


@pytest.mark.parametrize("initial_runner", ("openocd", "remote_openocd"))
def test_cmake_selection_does_not_depend_on_previous_validity(
    tmp_path: Path, initial_runner: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(f"default_runner: {initial_runner}\n")
    result, report = _configure_module(tmp_path, str(config), generator="Ninja")
    assert result.returncode == 0, result.stdout

    for contents, expected in (
        ("default_runnner: remote_openocd\n", None),
        ("default_runner: openocd\nremotes: false\n", "openocd"),
        ("default_runner: remote_openocd\nremotes: false\n", "remote_openocd"),
    ):
        config.write_text(contents)
        result = _build_module(report.parent, home=tmp_path / "home", override=str(config))
        if expected is None:
            assert result.returncode != 0
        else:
            assert result.returncode == 0, result.stdout
            assert report.read_text().splitlines() == [expected]


@pytest.mark.parametrize("text", ("", "# no settings\n", "---\n", "{}\n"))
def test_cmake_empty_configuration_uses_local_default(tmp_path: Path, text: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(text)

    result, report = _configure_module(tmp_path, str(config))

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == ["openocd"]


@pytest.mark.parametrize(
    "override",
    (
        "config.yaml",
        "./config.yaml",
        "../config.yaml",
        "~",
        "~/config.yaml",
        "~otheruser/config.yaml",
        "/configuration/../config.yaml",
    ),
)
def test_cmake_rejects_unsupported_configuration_override(tmp_path: Path, override: str) -> None:
    result, _ = _configure_module(tmp_path, override)

    assert result.returncode != 0
    assert "ZEPHYR_REMOTE_OPENOCD_CONFIG" in result.stdout
    assert "absolute" in result.stdout
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize("generator", ("Ninja", "Unix Makefiles"))
@pytest.mark.parametrize(
    "override_name",
    (None, "custom[*?].yaml"),
    ids=("default-location", "override-location"),
)
def test_cmake_config_presence_changes_regenerate_default_runner(
    tmp_path: Path, generator: str, override_name: str | None
) -> None:
    home = tmp_path / "home"
    if override_name is None:
        config = home / ".config" / "zephyr_remote_openocd" / "config.yaml"
        override = ""
    else:
        config = tmp_path / override_name
        override = str(config)

    result, report = _configure_module(tmp_path, override, generator=generator)

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == ["openocd"]

    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("default_runner: remote_openocd\n")
    result = _build_module(report.parent, home=home, override=override)

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == ["remote_openocd"]

    config.unlink()
    result = _build_module(report.parent, home=home, override=override)

    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == ["openocd"]


@pytest.mark.parametrize("generator", ("Ninja", "Unix Makefiles"))
@pytest.mark.parametrize(
    "config_relative",
    ("selected[*?].yaml", "lab;debug.yaml", "lab;debug/config.yaml"),
    ids=("glob-characters", "semicolon-filename", "semicolon-directory"),
)
def test_absolute_config_regenerates_from_different_cwd(
    tmp_path: Path, generator: str, config_relative: str
) -> None:
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (invocation / "~").symlink_to(target, target_is_directory=True)
    config = target / config_relative
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("default_runner: openocd\n")
    override = f"{invocation}/~/./{config_relative}"
    result, report = _configure_module(tmp_path, override, generator=generator, cwd=invocation)
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines()[-1] == "openocd"

    # Regeneration runs CMake from the build directory, with a different cwd
    # from both initial configuration and the build caller.
    for contents, expected in (
        ("default_runner: remote_openocd\n", "remote_openocd"),
        ("default_runner: openocd\n", "openocd"),
        ("default_runner: remote_openocd\n", "remote_openocd"),
        (None, "openocd"),
        ("default_runner: remote_openocd\n", "remote_openocd"),
    ):
        if contents is None:
            config.unlink()
        else:
            config.write_text(contents)
        result = _build_module(report.parent, home=tmp_path / "home", override=override)
        assert result.returncode == 0, result.stdout
        assert report.read_text().splitlines() == [expected]


def _age(path: Path, build: Path) -> None:
    """Make path older than the generated build system, as preserved timestamps can."""
    older = (build / "CMakeCache.txt").stat().st_mtime_ns - 3_600_000_000_000
    os.utime(path, ns=(older, older))


def _replace_symlink(link: Path, target: Path) -> None:
    replacement = link.with_name(link.name + ".new")
    replacement.symlink_to(target, target_is_directory=target.is_dir())
    replacement.replace(link)


@pytest.mark.parametrize("generator", ("Ninja", "Unix Makefiles"))
@pytest.mark.parametrize(
    "replacement", ("move-older-file", "retarget-file-symlink", "retarget-directory-symlink")
)
def test_older_configuration_replacement_never_leaves_stale_runner(
    tmp_path: Path, generator: str, replacement: str
) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    for directory, runner in ((local, "openocd"), (remote, "remote_openocd")):
        directory.mkdir()
        (directory / "config.yaml").write_text(f"default_runner: {runner}\n")
    if replacement == "retarget-directory-symlink":
        (tmp_path / "current").symlink_to(local, target_is_directory=True)
        config = tmp_path / "current" / "config.yaml"
    else:
        config = tmp_path / "config.yaml"
        if replacement == "move-older-file":
            config.write_text("default_runner: openocd\n")
        else:
            config.symlink_to(local / "config.yaml")
    result, report = _configure_module(tmp_path, str(config), generator=generator)
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == ["openocd"]

    build = report.parent
    _age(remote / "config.yaml", build)
    _age(remote, build)
    if replacement == "move-older-file":
        (remote / "config.yaml").replace(config)
    elif replacement == "retarget-file-symlink":
        _replace_symlink(config, remote / "config.yaml")
    else:
        _replace_symlink(tmp_path / "current", remote)

    result = _build_module(build, home=tmp_path / "home", override=str(config))
    if generator != "Ninja":
        # This generator cannot rerun CMake within the invocation that sees the
        # change, so that build must fail rather than leave the stale default.
        assert result.returncode != 0
        assert str(config) in result.stdout
        result = _build_module(build, home=tmp_path / "home", override=str(config))
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == ["remote_openocd"]

    configured = report.stat().st_mtime_ns
    result = _build_module(build, home=tmp_path / "home", override=str(config))
    assert result.returncode == 0, result.stdout
    assert report.stat().st_mtime_ns == configured


@pytest.mark.parametrize("generator", ("Ninja", "Unix Makefiles"))
def test_clean_build_detects_older_configuration_replacement(
    tmp_path: Path, generator: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("default_runner: openocd\n")
    result, report = _configure_module(tmp_path, str(config), generator=generator)
    assert result.returncode == 0, result.stdout
    build = report.parent
    result = _build_module(build, home=tmp_path / "home", override=str(config), target="clean")
    assert result.returncode == 0, result.stdout

    replacement = tmp_path / "remote.yaml"
    replacement.write_text("default_runner: remote_openocd\n")
    _age(replacement, build)
    replacement.replace(config)
    result = _build_module(build, home=tmp_path / "home", override=str(config))
    if generator != "Ninja":
        assert result.returncode != 0
        result = _build_module(build, home=tmp_path / "home", override=str(config))
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == ["remote_openocd"]


@pytest.mark.parametrize("generator", ("Ninja", "Unix Makefiles"))
def test_unreadable_configuration_fails_closed_without_timestamp_change(
    tmp_path: Path, generator: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("default_runner: remote_openocd\n")
    result, report = _configure_module(tmp_path, str(config), generator=generator)
    assert result.returncode == 0, result.stdout

    config.chmod(0)
    try:
        config.read_bytes()
    except PermissionError:
        pass
    else:
        pytest.skip("file permissions do not restrict this user")
    result = _build_module(report.parent, home=tmp_path / "home", override=str(config))
    if generator != "Ninja":
        assert result.returncode != 0
        result = _build_module(report.parent, home=tmp_path / "home", override=str(config))
    # CMake configuration reports the authoritative diagnostic.
    assert result.returncode != 0
    assert "cannot read configuration" in result.stdout

    config.chmod(0o600)
    result = _build_module(report.parent, home=tmp_path / "home", override=str(config))
    assert result.returncode == 0, result.stdout
    assert report.read_text().splitlines() == ["remote_openocd"]


def test_cmake_rejects_dangling_configuration_symlink(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.symlink_to(tmp_path / "missing.yaml")

    result, _ = _configure_module(tmp_path, str(config))

    assert result.returncode != 0
    assert str(config) in result.stdout
    assert "does not resolve to an existing file" in result.stdout
