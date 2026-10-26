# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.support import ROOT, env_path

pytestmark = pytest.mark.zephyr

try:
    import yaml
except ImportError:  # pragma: no cover - handled as an integration prerequisite
    HAVE_YAML = False
else:
    HAVE_YAML = True

OPENOCD_BOARD = "rpi_pico"
NON_OPENOCD_BOARD = "native_sim/native/64"


class TestZephyrIntegration:
    _scratch: tempfile.TemporaryDirectory[str]
    zephyr_base: Path
    openocd_board: str
    no_openocd_board: str
    west: Path
    config: Path
    scratch: Path
    fake_openocd: Path
    cache: Path
    build_in_tree: Path
    build_out_tree: Path
    build_without_openocd: Path
    app_out_tree: Path

    @classmethod
    def setup_class(cls) -> None:
        zephyr_base = env_path("ZEPHYR_BASE")
        cls.openocd_board = OPENOCD_BOARD
        cls.no_openocd_board = NON_OPENOCD_BOARD
        west_on_path = shutil.which("west")
        west = env_path("WEST") or (Path(west_on_path) if west_on_path else None)
        missing = []
        if zephyr_base is None or not zephyr_base.is_dir():
            missing.append("ZEPHYR_BASE")
        if west is None or not west.is_file():
            missing.append("WEST or west on PATH")
        if not HAVE_YAML:
            missing.append("PyYAML")
        if missing:
            pytest.skip("Zephyr integration prerequisites missing: " + ", ".join(missing))

        assert zephyr_base is not None and west is not None
        cls.zephyr_base = zephyr_base
        cls.west = west

        cls._scratch = tempfile.TemporaryDirectory(
            prefix="zephyr_integration_", dir=ROOT / ".scratch"
        )
        cls.scratch = Path(cls._scratch.name)
        cls.config = cls.scratch / "config.yaml"
        cls.fake_openocd = cls.scratch / "fake_openocd"
        cls.fake_openocd.write_text("#!/bin/sh\nprintf 'Open On-Chip Debugger 0.12.0\\n'\n")
        cls.fake_openocd.chmod(cls.fake_openocd.stat().st_mode | stat.S_IXUSR)
        cls.cache = cls.scratch / "zephyr_cache"
        cls.build_in_tree = cls.scratch / "build_in_tree"
        cls.build_out_tree = cls.scratch / "build_out_tree"
        cls.build_without_openocd = cls.scratch / "build_without_openocd"
        cls.app_out_tree = cls.scratch / "application"
        cls._write_config("openocd")

        sample = cls.zephyr_base / "samples" / "hello_world"
        cls._configure_recording_build(sample, cls.build_in_tree)
        shutil.copytree(sample, cls.app_out_tree)
        cls._configure_recording_build(cls.app_out_tree, cls.build_out_tree)
        cls._west(
            "build",
            "--cmake-only",
            "-b",
            cls.no_openocd_board,
            str(sample),
            "-d",
            str(cls.build_without_openocd),
            "--",
            f"-DUSER_CACHE_DIR={cls.cache}",
        )

    @classmethod
    def teardown_class(cls) -> None:
        scratch = getattr(cls, "_scratch", None)
        if scratch is not None:
            scratch.cleanup()

    @classmethod
    def _write_config(
        cls, default_runner: str, *, path: Path | None = None, host: str = "record_only"
    ) -> None:
        content = (
            f"default_runner: {default_runner}\n"
            "default_remote: record_only\n"
            "remotes:\n"
            "  record_only:\n"
            f"    ssh_host: {host}\n"
            "    openocd_command: [/remote/openocd]\n"
            "    ssh_command: [ssh]\n"
            "    path_mappings:\n"
            "      /: /recorded\n"
        )
        (path or cls.config).write_text(content)

    @classmethod
    def _west(
        cls,
        *args: str,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("ZEPHYR_REMOTE_OPENOCD_REMOTE", None)
        env.pop("ZRO_RECORD_OPENOCD_VERSION", None)
        env.update(
            {
                "EXTRA_ZEPHYR_MODULES": str(ROOT),
                "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(cls.config),
                "ZRO_RECORD": "1",
            }
        )
        env.update(extra_env or {})
        result = subprocess.run(
            [str(cls.west), *args],
            cwd=cwd if cwd is not None else cls.zephyr_base.parent,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        if check and result.returncode:
            raise AssertionError(
                f"west {' '.join(args)} failed ({result.returncode}):\n{result.stdout}"
            )
        return result

    @staticmethod
    def _runner_state(build: Path) -> dict[str, Any]:
        return yaml.safe_load((build / "zephyr" / "runners.yaml").read_text())

    @classmethod
    def _create_recording_flash_artifact(cls, build: Path) -> None:
        runner_config = cls._runner_state(build)["config"]
        artifact = build / "zephyr" / runner_config["hex_file"]
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.touch()

    @classmethod
    def _configure_recording_build(
        cls,
        application: Path,
        build: Path,
        *,
        extra_env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        """Configure real runner metadata and supply the no-build flash artifact."""
        cls._west(
            "build",
            "--cmake-only",
            "-b",
            cls.openocd_board,
            str(application),
            "-d",
            str(build),
            "--",
            f"-DUSER_CACHE_DIR={cls.cache}",
            f"-DOPENOCD={cls.fake_openocd}",
            extra_env=extra_env,
            cwd=cwd,
        )
        cls._create_recording_flash_artifact(build)

    @staticmethod
    def _recording(output: str) -> dict[str, Any]:
        start = output.find("{\n")
        if start < 0:
            raise AssertionError(f"recording JSON absent:\n{output}")
        return json.loads(output[start:])

    def test_runner_registration_is_conditional_and_non_destructive(self) -> None:
        state = self._runner_state(self.build_in_tree)
        assert state["args"]["remote_openocd"] == state["args"]["openocd"]
        enabled = state["runners"]
        disabled = self._runner_state(self.build_without_openocd)["runners"]
        assert enabled.count("remote_openocd") == 1
        assert "openocd" in enabled
        assert "remote_openocd" not in disabled
        self._west(
            "flash",
            "-d",
            str(self.build_in_tree),
            "-r",
            "openocd",
            "--context",
            "--no-rebuild",
        )

    @pytest.mark.parametrize(
        "application, command",
        (("in-tree", "flash"), ("in-tree", "debug"), ("out-of-tree", "flash")),
    )
    def test_discovered_module_reaches_adapter(self, application: str, command: str) -> None:
        build = self.build_in_tree if application == "in-tree" else self.build_out_tree
        assert str(ROOT) in (build / "zephyr_modules.txt").read_text()
        result = self._west(
            command,
            "-d",
            str(build),
            "-r",
            "remote_openocd",
            "--no-rebuild",
        )
        recording = self._recording(result.stdout)
        assert recording["command"] == command
        assert recording["remote_session_request"]["host"] == "record_only"
        config = recording["runner_config"]
        for field in ("board_dir", "elf_file", "gdb", "openocd"):
            assert config[field], field

    def test_config_change_regenerates_default_runner(self) -> None:
        state = self._runner_state(self.build_in_tree)
        assert state["flash-runner"] == "openocd"
        self._write_config("remote_openocd")
        self._west("build", "-d", str(self.build_in_tree), "-t", "help")
        result = self._west("flash", "-d", str(self.build_in_tree), "--no-rebuild")
        assert self._runner_state(self.build_in_tree)["flash-runner"] == "remote_openocd"
        assert self._recording(result.stdout)["command"] == "flash"

        self._write_config("openocd")
        self._west("build", "-d", str(self.build_in_tree), "-t", "help")
        result = self._west("flash", "-d", str(self.build_in_tree), "--no-rebuild")
        assert self._runner_state(self.build_in_tree)["flash-runner"] == "openocd"
        # Only remote_openocd honors recording; the default built-in runner ran instead.
        with pytest.raises(AssertionError, match="recording JSON absent"):
            self._recording(result.stdout)

    def test_created_config_regenerates_default_runner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="zro_created_config_", dir=ROOT / ".scratch"
        ) as directory:
            root = Path(directory)
            build = root / "build"
            config = root / "created-config.yaml"
            config_environment = {"ZEPHYR_REMOTE_OPENOCD_CONFIG": str(config)}
            sample = self.zephyr_base / "samples" / "hello_world"
            self._configure_recording_build(sample, build, extra_env=config_environment)
            assert self._runner_state(build)["flash-runner"] == "openocd"

            self._write_config("remote_openocd", path=config)
            self._west(
                "build",
                "-d",
                str(build),
                "-t",
                "help",
                extra_env=config_environment,
            )
            result = self._west(
                "flash",
                "-d",
                str(build),
                "--no-rebuild",
                extra_env=config_environment,
            )

            assert self._runner_state(build)["flash-runner"] == "remote_openocd"
            assert self._recording(result.stdout)["command"] == "flash"

    def test_older_config_replacement_regenerates_default_runner(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="zro_older_config_", dir=ROOT / ".scratch"
        ) as directory:
            root = Path(directory)
            build = root / "build"
            config = root / "config.yaml"
            remote = root / "remote.yaml"
            self._write_config("openocd", path=root / "local.yaml")
            self._write_config("remote_openocd", path=remote)
            config.symlink_to(root / "local.yaml")
            config_environment = {"ZEPHYR_REMOTE_OPENOCD_CONFIG": str(config)}
            sample = self.zephyr_base / "samples" / "hello_world"
            self._configure_recording_build(sample, build, extra_env=config_environment)
            assert self._runner_state(build)["flash-runner"] == "openocd"

            # Switch profiles to a file older than the generated build system.
            older = (build / "CMakeCache.txt").stat().st_mtime_ns - 3_600_000_000_000
            os.utime(remote, ns=(older, older))
            replacement = root / "config.yaml.new"
            replacement.symlink_to(remote)
            replacement.replace(config)
            self._west("build", "-d", str(build), "-t", "help", extra_env=config_environment)
            result = self._west(
                "flash", "-d", str(build), "--no-rebuild", extra_env=config_environment
            )

            assert self._runner_state(build)["flash-runner"] == "remote_openocd"
            assert self._recording(result.stdout)["command"] == "flash"

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
    def test_no_rebuild_runner_rejects_unsupported_override(self, override: str) -> None:
        result = self._west(
            "flash",
            "-d",
            str(self.build_out_tree),
            "-r",
            "remote_openocd",
            "--no-rebuild",
            extra_env={"ZEPHYR_REMOTE_OPENOCD_CONFIG": override},
            check=False,
            cwd=self.scratch,
        )

        assert result.returncode != 0
        assert "ZEPHYR_REMOTE_OPENOCD_CONFIG" in result.stdout
        assert "absolute" in result.stdout

    def test_absolute_override_is_independent_of_cwd_and_regeneration(self) -> None:
        root = self.scratch / "override-resolution"
        initial = root / "initial"
        runtime = root / "runtime"
        target = root / "~"
        for directory in (initial, runtime, target):
            directory.mkdir(parents=True)
        # A caller-relative interpretation would select this local decoy.
        (initial / "config.yaml").write_text("default_runner: openocd\n")
        config = target / "lab;debug.yaml"
        override = f"{target}/./{config.name}"
        environment = {"ZEPHYR_REMOTE_OPENOCD_CONFIG": override}
        build = root / "build"
        self._write_config("remote_openocd", path=config)
        self._configure_recording_build(
            self.app_out_tree,
            build,
            extra_env=environment,
            cwd=initial,
        )
        assert self._runner_state(build)["flash-runner"] == "remote_openocd"

        def invoke() -> dict[str, Any]:
            result = self._west(
                "flash",
                "-d",
                str(build),
                "-r",
                "remote_openocd",
                "--no-rebuild",
                extra_env=environment,
                cwd=runtime,
            )
            return self._recording(result.stdout)

        assert invoke()["selected_config"]["path"] == override
        # --no-rebuild reads current settings but deliberately keeps old defaults.
        self._write_config("openocd", path=config, host="updated_record_only")
        assert invoke()["remote_session_request"]["host"] == "updated_record_only"
        assert self._runner_state(build)["flash-runner"] == "remote_openocd"

        for contents, expected in (
            ("openocd", "openocd"),
            (None, "openocd"),
            ("remote_openocd", "remote_openocd"),
        ):
            if contents is None:
                config.unlink()
            else:
                self._write_config(contents, path=config)
            self._west("build", "-d", str(build), "-t", "help", extra_env=environment, cwd=runtime)
            assert self._runner_state(build)["flash-runner"] == expected

        result = self._west(
            "flash",
            "-d",
            str(build),
            "--no-rebuild",
            extra_env=environment,
            cwd=runtime,
        )
        assert self._recording(result.stdout)["remote_session_request"]["host"] == "record_only"

        config.unlink()
        config.symlink_to(root / "missing.yaml")
        result = self._west(
            "flash",
            "-d",
            str(build),
            "--no-rebuild",
            extra_env=environment,
            cwd=runtime,
            check=False,
        )
        assert result.returncode != 0
        assert "does not resolve to an existing file" in result.stdout
        result = self._west(
            "build",
            "-d",
            str(build),
            "--cmake-only",
            extra_env=environment,
            cwd=initial,
            check=False,
        )
        assert result.returncode != 0
        assert "does not resolve to an existing file" in result.stdout

    def test_clean_install_acceptance_from_git_free_distribution(self) -> None:
        """A copied module works through EXTRA_ZEPHYR_MODULES alone."""
        with tempfile.TemporaryDirectory(prefix="zro_clean_install_") as directory:
            root = Path(directory)
            distribution = root / "zephyr_remote_openocd"
            build = root / "build"
            cache = root / "cache"
            config = root / "config.yaml"
            fake_openocd = root / "fake_openocd"
            ignored = shutil.ignore_patterns(
                ".git",
                ".scratch",
                ".venv",
                ".mypy_cache",
                ".ruff_cache",
                ".pytest_cache",
                ".coverage",
                "build",
                "dist",
                "*.egg-info",
                "__pycache__",
                "*.pyc",
            )
            shutil.copytree(ROOT, distribution, ignore=ignored)
            assert not (distribution / ".git").exists()
            assert not str(distribution).startswith(str(ROOT))
            fake_openocd.write_text("#!/bin/sh\nprintf 'Open On-Chip Debugger 0.12.0\\n'\n")
            fake_openocd.chmod(fake_openocd.stat().st_mode | stat.S_IXUSR)
            config.write_text(
                "default_remote: record_only\n"
                "remotes:\n"
                "  record_only:\n"
                "    ssh_host: record_only\n"
                "    openocd_command: [/remote/openocd]\n"
                "    ssh_command: [ssh]\n"
                '    path_mappings: {"/": /recorded}\n'
            )

            clean_env = os.environ.copy()
            for name in (
                "PYTHONPATH",
                "ZEPHYR_REMOTE_OPENOCD_CONFIG",
                "ZEPHYR_REMOTE_OPENOCD_REMOTE",
                "ZRO_RECORD",
                "ZRO_RECORD_OPENOCD_VERSION",
                "EXTRA_ZEPHYR_MODULES",
                "ZEPHYR_EXTRA_MODULES",
                "ZEPHYR_MODULES",
            ):
                clean_env.pop(name, None)
            clean_env.update(
                {
                    "EXTRA_ZEPHYR_MODULES": str(distribution),
                    "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(config),
                    "ZRO_RECORD": "1",
                }
            )

            def west(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                result = subprocess.run(
                    [str(self.west), *args],
                    cwd=self.zephyr_base.parent,
                    env=clean_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=180,
                )
                if check and result.returncode:
                    raise AssertionError(
                        f"west {' '.join(args)} failed ({result.returncode}):\n{result.stdout}"
                    )
                return result

            sample = self.zephyr_base / "samples" / "hello_world"
            west(
                "build",
                "--cmake-only",
                "-b",
                self.openocd_board,
                str(sample),
                "-d",
                str(build),
                "--",
                f"-DUSER_CACHE_DIR={cache}",
                f"-DOPENOCD={fake_openocd}",
            )
            modules = (build / "zephyr_modules.txt").read_text()
            assert str(distribution) in modules
            assert str(ROOT) not in modules
            self._create_recording_flash_artifact(build)
            recorded = west(
                "flash",
                "-d",
                str(build),
                "-r",
                "remote_openocd",
                "--no-rebuild",
            )
            recording = self._recording(recorded.stdout)
            assert recording["command"] == "flash"
            assert recording["remote_session_request"]["host"] == "record_only"
