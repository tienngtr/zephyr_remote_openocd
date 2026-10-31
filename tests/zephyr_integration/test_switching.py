# SPDX-License-Identifier: Apache-2.0

"""Real west subprocesses across source switches with one unchanged ZRO tree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.support import ROOT, env_path

pytestmark = pytest.mark.zephyr


@dataclass(frozen=True)
class SwitchWorkspace:
    root: Path
    zephyr: Path
    env: dict[str, str]

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=self.root,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=300,
        )

    def west(self, *argv: str, fails: bool = False) -> str:
        result = self.run(sys.executable, "-m", "west", *argv)
        assert (result.returncode != 0) == fails, result.stdout
        return result.stdout

    def switch(self, ref: str) -> None:
        result = self.run("git", "-C", str(self.zephyr), "checkout", "--detach", ref)
        assert result.returncode == 0, result.stdout

    def configure(self, build: Path) -> None:
        args = [
            "build",
            "--cmake-only",
            "-b",
            "rpi_pico2/rp2350a/m33",
            str(self.zephyr / "samples/hello_world"),
            "-d",
            str(build),
            "--",
            f"-DUSER_CACHE_DIR={self.root / 'cache'}",
            f"-DOPENOCD={self.root / 'openocd'}",
        ]
        modules = self.env.get("ZRO_SWITCH_MODULES")
        if modules:
            args.append(f"-DZEPHYR_MODULES={modules}")
        self.west(*args)

    def metadata(self, build: Path) -> dict[str, Any]:
        data = yaml.safe_load((build / "zephyr/runners.yaml").read_text())
        assert data["args"]["remote_openocd"] == data["args"]["openocd"]
        assert data["runners"].count("remote_openocd") == 1
        return data

    def record(self, build: Path, command: str) -> dict[str, Any]:
        data = self.metadata(build)
        # Explicit no-build artifact: recording does not execute firmware.
        (build / "zephyr" / data["config"]["hex_file"]).touch()
        output = self.west(command, "-d", str(build), "--no-rebuild")
        return json.loads(output[output.index("{\n") :])

    def probe_registration(self, family: str) -> None:
        result = self.run(
            sys.executable,
            str(ROOT / "tests/fixtures/runner_import_probe.py"),
            str(self.zephyr),
            str(ROOT),
            family,
        )
        assert result.returncode == 0, result.stdout


@pytest.fixture
def switch_workspace() -> Iterator[SwitchWorkspace]:
    source = env_path("ZRO_SWITCH_ZEPHYR_REPOSITORY") or env_path("ZEPHYR_BASE")
    if source is None:
        pytest.skip("ZEPHYR_BASE or ZRO_SWITCH_ZEPHYR_REPOSITORY is required")
    for ref in ("v4.4.0", "v4.5.0"):
        check = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        if check.returncode:
            pytest.skip(f"switching repository must contain {ref}")
    pytest.importorskip("west")
    with tempfile.TemporaryDirectory(prefix="zephyr_switch_", dir=ROOT / ".scratch") as scratch:
        workspace = Path(scratch)
        zephyr = workspace / "zephyr"
        clone = subprocess.run(
            ["git", "clone", "--shared", "--no-checkout", str(source), str(zephyr)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert clone.returncode == 0, clone.stderr
        (workspace / ".west").mkdir()
        (workspace / ".west/config").write_text(
            "[manifest]\npath = zephyr\n"
            "project-filter = -.*,+cmsis,+cmsis_6,+hal_rpi_pico\n"
            "[zephyr]\nbase = zephyr\n"
        )
        modules = source.parent / "modules"
        if modules.exists():
            (workspace / "modules").symlink_to(modules, target_is_directory=True)
        config = workspace / "remote.yaml"
        config.write_text(
            "default_runner: remote_openocd\ndefault_remote: record\nremotes:\n"
            "  record:\n    ssh_host: record_only\n    openocd_command: [/remote/openocd]\n"
            "    ssh_command: [ssh]\n    path_mappings: {'/': /recorded}\n"
        )
        executable = workspace / "openocd"
        executable.write_text("#!/bin/sh\nexit 99\n")
        executable.chmod(0o755)
        env = os.environ.copy()
        for key in (
            "ZEPHYR_BASE",
            "ZEPHYR_MODULES",
            "EXTRA_ZEPHYR_MODULES",
            "ZEPHYR_EXTRA_MODULES",
            "ZEPHYR_REMOTE_OPENOCD_REMOTE",
            "PYTHONPATH",
        ):
            env.pop(key, None)
        env.update(
            EXTRA_ZEPHYR_MODULES=str(ROOT),
            ZEPHYR_REMOTE_OPENOCD_CONFIG=str(config),
            ZRO_RECORD="1",
            CCACHE_DIR=str(workspace / "ccache"),
        )
        yield SwitchWorkspace(workspace, zephyr, env)


def test_source_switching_regenerates_shared_and_preserves_separate_builds(
    switch_workspace: SwitchWorkspace,
) -> None:
    ws = switch_workspace
    shared = ws.root / "shared"
    separate44 = ws.root / "build44"
    separate45 = ws.root / "build45"
    ws.switch("v4.4.0")
    ws.configure(shared)
    ws.configure(separate44)
    ws.probe_registration("44")
    assert "--gdb-pre-debug" not in ws.metadata(shared)["args"]["remote_openocd"]
    assert ws.record(separate44, "flash")["command"] == "flash"

    ws.switch("v4.5.0")
    ws.probe_registration("45")  # Probe before rebuilding the old 4.4 directory.
    ws.west("build", "-d", str(shared), "-t", "help")
    ws.configure(separate45)
    assert "--gdb-pre-debug" in ws.metadata(shared)["args"]["remote_openocd"]
    for build in (shared, separate45):
        for command in ("flash", "debug"):
            assert ws.record(build, command)["command"] == command
    log = ws.west(
        "debug", "-d", str(separate45), "--no-rebuild", "--log-file=output.log", fails=True
    )
    assert "--log-file is not supported" in log
    ws.west(
        "build",
        "--cmake-only",
        "-d",
        str(separate45),
        "--",
        "-DBOARD_RUNNER_ARGS_openocd=--log-file=board.log",
    )
    assert "--log-file=board.log" in ws.metadata(separate45)["args"]["remote_openocd"]
    board_log = ws.west("flash", "-d", str(separate45), "--no-rebuild", fails=True)
    assert "--log-file is not supported" in board_log

    ws.switch("v4.4.0")
    ws.probe_registration("44")  # Old 4.5 metadata cannot choose the adapter.
    for runner in ("openocd", "remote_openocd"):
        stale = ws.west("debug", "-r", runner, "-d", str(shared), "--no-rebuild", fails=True)
        assert "unknown arguments" in stale and "--gdb-pre-debug" in stale
    ws.west("build", "-d", str(shared), "-t", "help")
    assert "--gdb-pre-debug" not in ws.metadata(shared)["args"]["remote_openocd"]
    for build in (shared, separate44):
        for command in ("flash", "debug"):
            assert ws.record(build, command)["command"] == command


def test_unsupported_source_keeps_stock_runner_usable(switch_workspace: SwitchWorkspace) -> None:
    ws = switch_workspace
    ws.switch("v4.4.0")
    build = ws.root / "build"
    ws.configure(build)
    version = ws.zephyr / "VERSION"
    # These labels use the same real upstream API; the selected source version
    # alone determines whether ZRO is available. No Zephyr classes are mocked.
    for contents in (
        "VERSION_MAJOR = 4\nVERSION_MINOR = 5\nPATCHLEVEL = 99\n"
        "VERSION_TWEAK = 0\nEXTRAVERSION =\n",
        "VERSION_MAJOR = 4\nVERSION_MINOR = 5\nPATCHLEVEL = 0\n"
        "VERSION_TWEAK = 0\nEXTRAVERSION = rc1\n",
        "VERSION_MAJOR = 5\nVERSION_MINOR = 0\nPATCHLEVEL = 0\nVERSION_TWEAK = 0\nEXTRAVERSION =\n",
        "invalid version",
        None,
    ):
        if contents is None:
            version.unlink()
        else:
            version.write_text(contents)
        ws.probe_registration("unsupported")
        remote = ws.west("flash", "-d", str(build), "--no-rebuild", fails=True)
        assert "supported versions are stable Zephyr 4.4.x and 4.5.x" in remote
        # A real stock parser failure proves external import did not reject the
        # invocation; the unknown option prevents any hardware execution.
        stock = ws.west(
            "flash",
            "-r",
            "openocd",
            "-d",
            str(build),
            "--no-rebuild",
            "--unknown-investigation-option",
            fails=True,
        )
        assert "runner openocd received unknown arguments" in stock
        assert "unsupported Zephyr" not in stock
