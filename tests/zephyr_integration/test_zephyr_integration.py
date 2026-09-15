# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.support import ROOT, env_path

pytestmark = pytest.mark.zephyr

try:
    import yaml
except ImportError:  # pragma: no cover - handled as an integration prerequisite
    yaml = None


class TestZephyrIntegration:
    zephyr_base: Path
    openocd_board: str
    no_openocd_board: str
    west: Path
    config: Path
    ccache: Path
    ccache_tmp: Path

    @classmethod
    def setup_class(cls):
        cls.zephyr_base = env_path("ZEPHYR_BASE")
        cls.openocd_board = os.environ.get("OPENOCD_TEST_BOARD")
        cls.no_openocd_board = os.environ.get("NON_OPENOCD_TEST_BOARD", "native_sim/native/64")
        cls.west = env_path("WEST") or (
            Path(shutil.which("west")) if shutil.which("west") else None
        )
        missing = []
        if cls.zephyr_base is None or not cls.zephyr_base.is_dir():
            missing.append("ZEPHYR_BASE")
        if cls.west is None or not cls.west.is_file():
            missing.append("WEST or west on PATH")
        if not cls.openocd_board:
            missing.append("OPENOCD_TEST_BOARD")
        if yaml is None:
            missing.append("PyYAML")
        if missing:
            pytest.skip("Zephyr integration prerequisites missing: " + ", ".join(missing))

        cls._scratch = tempfile.TemporaryDirectory(
            prefix="zephyr_integration_", dir=ROOT / ".scratch"
        )
        cls.scratch = Path(cls._scratch.name)
        cls.config = cls.scratch / "config.yaml"
        cls.fake_openocd = cls.scratch / "fake_openocd"
        cls.fake_openocd.write_text("#!/bin/sh\nprintf 'Open On-Chip Debugger 0.12.0\\n'\n")
        cls.fake_openocd.chmod(cls.fake_openocd.stat().st_mode | stat.S_IXUSR)
        cls.cache = cls.scratch / "zephyr_cache"
        cls.ccache = cls.scratch / "ccache"
        cls.ccache_tmp = cls.scratch / "ccache_tmp"
        cls.build_in_tree = cls.scratch / "build_in_tree"
        cls.build_out_tree = cls.scratch / "build_out_tree"
        cls.build_without_openocd = cls.scratch / "build_without_openocd"
        cls.app_out_tree = cls.scratch / "application"
        cls._write_config("openocd")

        sample = cls.zephyr_base / "samples" / "hello_world"
        cls._west(
            "build",
            "-b",
            cls.openocd_board,
            str(sample),
            "-d",
            str(cls.build_in_tree),
            "--",
            f"-DUSER_CACHE_DIR={cls.cache}",
            f"-DOPENOCD={cls.fake_openocd}",
        )
        shutil.copytree(sample, cls.app_out_tree)
        cls._west(
            "build",
            "-b",
            cls.openocd_board,
            str(cls.app_out_tree),
            "-d",
            str(cls.build_out_tree),
            "--",
            f"-DUSER_CACHE_DIR={cls.cache}",
            f"-DOPENOCD={cls.fake_openocd}",
        )
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
    def teardown_class(cls):
        scratch = getattr(cls, "_scratch", None)
        if scratch is not None:
            scratch.cleanup()

    @classmethod
    def _write_config(cls, default_runner: str):
        content = (
            f"default_runner: {default_runner}\n"
            "default_remote: record_only\n"
            "remotes:\n"
            "  record_only:\n"
            "    ssh_host: record_only\n"
            "    openocd_command: [/remote/openocd]\n"
            "    ssh_command: [ssh]\n"
            "    path_mappings:\n"
            "      /: /recorded\n"
        )
        cls.config.write_text(content)

    @classmethod
    def _west(cls, *args: str, check: bool = True, extra_env=None):
        env = os.environ.copy()
        env.pop("ZEPHYR_REMOTE_OPENOCD_REMOTE", None)
        env.pop("ZRO_RECORD_VERSION", None)
        env.update(
            {
                "EXTRA_ZEPHYR_MODULES": str(ROOT),
                "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(cls.config),
                "ZRO_RECORD": "1",
                "CCACHE_DIR": str(cls.ccache),
                "CCACHE_TEMPDIR": str(cls.ccache_tmp),
            }
        )
        env.update(extra_env or {})
        result = subprocess.run(
            [str(cls.west), *args],
            cwd=cls.zephyr_base.parent,
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
    def _runner_state(build: Path):
        return yaml.safe_load((build / "zephyr" / "runners.yaml").read_text())

    @staticmethod
    def _recording(output: str):
        start = output.find("{\n")
        if start < 0:
            raise AssertionError(f"recording JSON absent:\n{output}")
        return json.loads(output[start:])

    def test_module_discovery_and_in_tree_application_build(self):
        modules = (self.build_in_tree / "zephyr_modules.txt").read_text()
        assert str(ROOT) in modules
        assert (self.build_in_tree / "zephyr" / "zephyr.elf").is_file()

    def test_out_of_tree_application_build(self):
        assert not str(self.app_out_tree).startswith(str(self.zephyr_base))
        assert (self.build_out_tree / "zephyr" / "zephyr.elf").is_file()

    def test_runner_registration_is_conditional_and_non_destructive(self):
        enabled = self._runner_state(self.build_in_tree)["runners"]
        disabled = self._runner_state(self.build_without_openocd)["runners"]
        assert enabled.count("remote_openocd") == 1
        assert "openocd" in enabled
        assert "remote_openocd" not in disabled
        context = self._west("flash", "-d", str(self.build_in_tree), "-r", "openocd", "--context")
        assert "openocd capabilities:" in context.stdout

    def test_openocd_arguments_are_mirrored_exactly(self):
        args = self._runner_state(self.build_in_tree)["args"]
        assert args["remote_openocd"] == args["openocd"]

    @pytest.mark.parametrize("command", ("flash", "debug"))
    def test_recording_smoke_reaches_adapter(self, command):
        result = self._west(
            command,
            "-d",
            str(self.build_in_tree),
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

    def test_config_change_regenerates_default_runner(self):
        state = self._runner_state(self.build_in_tree)
        assert state["flash-runner"] == "openocd"
        self._write_config("remote_openocd")
        self._west("flash", "-d", str(self.build_in_tree))
        assert self._runner_state(self.build_in_tree)["flash-runner"] == "remote_openocd"

        self._write_config("openocd")
        self._west("flash", "-d", str(self.build_in_tree))
        assert self._runner_state(self.build_in_tree)["flash-runner"] == "openocd"

    @staticmethod
    def _west_python(west: Path) -> Path:
        """Return West's interpreter so setup checks its Zephyr dependencies."""
        try:
            first_line = west.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, UnicodeDecodeError, IndexError):
            return Path(sys.executable)
        if not first_line.startswith("#!"):
            return Path(sys.executable)
        command = first_line[2:].strip().split()
        if not command:
            return Path(sys.executable)
        if Path(command[0]).name == "env":
            resolved = shutil.which(command[-1])
            return Path(resolved) if resolved else Path(sys.executable)
        candidate = Path(command[0])
        return candidate if candidate.is_file() else Path(sys.executable)

    def test_clean_install_acceptance_from_git_free_distribution(self):
        """A copied module works through setup and EXTRA_ZEPHYR_MODULES alone."""
        with tempfile.TemporaryDirectory(prefix="zro_clean_install_") as directory:
            root = Path(directory)
            distribution = root / "zephyr_remote_openocd"
            home = root / "home"
            build = root / "build"
            cache = root / "cache"
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
            shutil.rmtree(distribution / "docs")
            assert not (distribution / "docs").exists()
            assert not str(distribution).startswith(str(ROOT))
            fake_openocd.write_text("#!/bin/sh\nprintf 'Open On-Chip Debugger 0.12.0\\n'\n")
            fake_openocd.chmod(fake_openocd.stat().st_mode | stat.S_IXUSR)

            clean_env = os.environ.copy()
            for name in (
                "PYTHONPATH",
                "ZEPHYR_REMOTE_OPENOCD_CONFIG",
                "ZEPHYR_REMOTE_OPENOCD_REMOTE",
                "ZRO_RECORD",
                "ZRO_RECORD_VERSION",
                "EXTRA_ZEPHYR_MODULES",
                "ZEPHYR_EXTRA_MODULES",
                "ZEPHYR_MODULES",
            ):
                clean_env.pop(name, None)
            clean_env.update(
                {
                    "HOME": str(home),
                    "EXTRA_ZEPHYR_MODULES": str(distribution),
                    "CCACHE_DIR": str(root / "ccache"),
                    "CCACHE_TEMPDIR": str(root / "ccache_tmp"),
                }
            )
            zephyr_python = self._west_python(self.west)
            assert (
                subprocess.run(
                    [str(zephyr_python), "-c", "import elftools"],
                    env=clean_env,
                    check=False,
                ).returncode
                == 0
            ), "the Zephyr Python environment must provide pyelftools"
            setup_command = [str(zephyr_python), str(distribution / "scripts" / "setup.py")]
            first_setup = subprocess.run(
                setup_command,
                cwd=distribution,
                env=clean_env,
                text=True,
                capture_output=True,
                check=False,
            )
            assert first_setup.returncode == 0, first_setup.stderr
            config = home / ".config" / "zephyr_remote_openocd" / "config.yaml"
            assert (
                config.read_bytes()
                == (distribution / "resources" / "config.example.yaml").read_bytes()
            )
            assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(config.stat().st_mode) == 0o600
            assert f"Configuration (created): {config}" in first_setup.stdout
            assert f"Module root: {distribution}" in first_setup.stdout
            assert "pyelftools: found" in first_setup.stdout
            assert "YAML configuration dependencies: found" in first_setup.stdout
            assert str(ROOT) not in first_setup.stdout

            config.write_text(
                'default_runner: openocd\n'
                'default_remote: record_only\n'
                'remotes:\n  record_only:\n'
                '    ssh_host: record_only\n'
                '    openocd_command: [/remote/openocd]\n'
                '    ssh_command: [ssh]\n'
                '    path_mappings: {"/": /recorded}\n'
            )
            configured_contents = config.read_bytes()
            second_setup = subprocess.run(
                setup_command,
                cwd=root,
                env=clean_env,
                text=True,
                capture_output=True,
                check=False,
            )
            assert second_setup.returncode == 0, second_setup.stderr
            assert config.read_bytes() == configured_contents
            assert f"Configuration (already exists): {config}" in second_setup.stdout

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
                        f"west {' '.join(args)} failed ({result.returncode}):\\n{result.stdout}"
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
            initial = self._runner_state(build)
            assert "openocd" in initial["runners"]
            assert "remote_openocd" in initial["runners"]
            assert initial["flash-runner"] == "openocd"
            local_context = west("flash", "-d", str(build), "--context")
            assert "openocd capabilities:" in local_context.stdout

            config.write_text(
                config.read_text().replace(
                    "default_runner: openocd", "default_runner: remote_openocd"
                )
            )
            clean_env["ZRO_RECORD"] = "1"
            recorded = west("flash", "-d", str(build))
            assert self._runner_state(build)["flash-runner"] == "remote_openocd"
            recording = self._recording(recorded.stdout)
            assert recording["command"] == "flash"
            assert recording["remote_session_request"]["host"] == "record_only"
