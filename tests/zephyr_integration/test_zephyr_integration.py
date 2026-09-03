# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
import json
import os
import re
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
    """Permanent coverage for retired prototype gates PG-001 through PG-010."""

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
            prefix="zephyr-integration-", dir=ROOT / ".scratch"
        )
        cls.scratch = Path(cls._scratch.name)
        cls.config = cls.scratch / "config.toml"
        cls.fake_openocd = cls.scratch / "fake-openocd"
        cls.fake_openocd.write_text("#!/bin/sh\nprintf 'Open On-Chip Debugger 0.12.0\\n'\n")
        cls.fake_openocd.chmod(cls.fake_openocd.stat().st_mode | stat.S_IXUSR)
        cls.cache = cls.scratch / "zephyr-cache"
        cls.ccache = cls.scratch / "ccache"
        cls.ccache_tmp = cls.scratch / "ccache-tmp"
        cls.build_in_tree = cls.scratch / "build-in-tree"
        cls.build_out_tree = cls.scratch / "build-out-tree"
        cls.build_thread_info = cls.scratch / "build-thread-info"
        cls.build_without_openocd = cls.scratch / "build-without-openocd"
        cls.app_out_tree = cls.scratch / "application"
        cls._write_config("local")

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
        cls._west(
            "build",
            "-b",
            cls.openocd_board,
            str(sample),
            "-d",
            str(cls.build_thread_info),
            "--",
            f"-DUSER_CACHE_DIR={cls.cache}",
            f"-DOPENOCD={cls.fake_openocd}",
            "-DCONFIG_DEBUG_THREAD_INFO=y",
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
    def _write_config(cls, selected: str, forward_env: tuple[str, ...] = ()):
        content = (
            f'[zephyr]\ndefault = "{selected}"\n\n'
            '[remote]\nhost = "record-only"\nopenocd = "/remote/openocd"\n\n'
            '[ssh]\ncommand = ["ssh"]\n\n'
            '[[paths.map]]\nlocal = "/"\nremote = "/recorded"\n'
        )
        if forward_env:
            names = ", ".join(f'"{name}"' for name in forward_env)
            content += f"\n[openocd]\nforward_env = [{names}]\n"
        cls.config.write_text(content)

    @classmethod
    def _west(cls, *args: str, check: bool = True, extra_env=None):
        env = os.environ.copy()
        env.update(
            {
                "EXTRA_ZEPHYR_MODULES": str(ROOT),
                "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(cls.config),
                "ZEPHYR_REMOTE_OPENOCD_RECORD": "1",
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
        """Regression coverage for prototype gates PG-001 and PG-002."""
        modules = (self.build_in_tree / "zephyr_modules.txt").read_text()
        assert str(ROOT) in modules
        assert (self.build_in_tree / "zephyr" / "zephyr.elf").is_file()

    def test_out_of_tree_application_build(self):
        """Regression coverage for prototype gate PG-003."""
        assert not str(self.app_out_tree).startswith(str(self.zephyr_base))
        assert (self.build_out_tree / "zephyr" / "zephyr.elf").is_file()

    def test_runner_registration_is_conditional_and_non_destructive(self):
        """Regression coverage for prototype gates PG-004 and PG-007."""
        enabled = self._runner_state(self.build_in_tree)["runners"]
        disabled = self._runner_state(self.build_without_openocd)["runners"]
        assert enabled.count("remote-openocd") == 1
        assert "openocd" in enabled
        assert "remote-openocd" not in disabled
        context = self._west("flash", "-d", str(self.build_in_tree), "-r", "openocd", "--context")
        assert "openocd capabilities:" in context.stdout

    def test_openocd_arguments_are_mirrored_exactly(self):
        """Regression coverage for prototype gate PG-005."""
        args = self._runner_state(self.build_in_tree)["args"]
        assert args["remote-openocd"] == args["openocd"]

    def test_recording_commands_receive_runner_config_without_io(self):
        """Regression coverage for prototype gates PG-006 and PG-008."""
        for command in ("flash", "debug", "attach", "debugserver"):
            result = self._west(
                command,
                "-d",
                str(self.build_in_tree),
                "-r",
                "remote-openocd",
                "--no-rebuild",
            )
            recording = self._recording(result.stdout)
            assert recording["command"] == command
            config = recording["runner_config"]
            for required in ("board_dir", "elf_file", "gdb", "openocd"):
                assert config[required], required
            for optional in ("hex_file", "bin_file", "openocd_search"):
                assert optional in config
            if command == "flash":
                request = recording["remote_session_request"]
                assert request["host"] == "record-only"
                argv = request["process"]["argv"]
                assert argv[0] == "/remote/openocd"
                assert "bindto {address}" in argv
                assert not any(" disabled" in argument for argument in argv)
                if recording["runner_args"].get("file_type") == "elf":
                    assert any(argument.startswith("load_image ") for argument in argv)
                else:
                    assert any(argument.startswith("flash write_image ") for argument in argv)
                assert request["process"]["environment"] == []
            else:
                request = recording["remote_session_request"]
                assert [
                    (item["name"], item["local_port"], item["remote_port"])
                    for item in request["services"]
                ] == [("gdb", 3333, 3333), ("tcl", 6333, 6333), ("telnet", 4444, 4444)]
                assert re.match(r"^ZRO_READY_[0-9a-f]{32}$", request["process"]["readiness_marker"])
                if command == "debugserver":
                    assert recording["local_gdb_argv"] is None
                    assert "reset init" in request["process"]["argv"]
                else:
                    client = recording["local_gdb_argv"]
                    assert "target extended-remote 127.0.0.1:3333" in client
                    assert ("load" in client) == (command == "debug")

    def test_recording_forwards_only_present_allow_list_environment(self):
        selected = "ZRO_TEST_PROBE_CHANNEL"
        missing = "ZRO_TEST_ABSENT_ENVIRONMENT"
        prior_missing = os.environ.pop(missing, None)
        try:
            self._write_config("remote", (selected, missing))
            try:
                result = self._west(
                    "flash",
                    "-d",
                    str(self.build_in_tree),
                    "-r",
                    "remote-openocd",
                    "--no-rebuild",
                    extra_env={selected: "channel-1", "ZRO_TEST_UNLISTED": "not-forwarded"},
                )
                recording = self._recording(result.stdout)
                assert recording["remote_session_request"]["process"]["environment"] == [selected]
                assert (
                    f"allow-listed environment variable {missing} is absent; omitting it"
                    in result.stdout
                )
            finally:
                self._write_config("local")
        finally:
            if prior_missing is not None:
                os.environ[missing] = prior_missing

    def test_recording_thread_info_uses_injected_remote_version(self):
        result = self._west(
            "debug",
            "-d",
            str(self.build_thread_info),
            "-r",
            "remote-openocd",
            "--no-rebuild",
            extra_env={"ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION": "Open On-Chip Debugger 0.12.0"},
        )
        recording = self._recording(result.stdout)
        assert recording["thread_info"] == {
            "requested": True,
            "version": "Open On-Chip Debugger 0.12.0",
            "version_source": "injected",
            "rtos_awareness": True,
        }
        assert (
            "$_TARGETNAME configure -rtos Zephyr"
            in recording["remote_session_request"]["process"]["argv"]
        )

    def test_recording_thread_info_requires_injected_version(self):
        result = self._west(
            "debug",
            "-d",
            str(self.build_thread_info),
            "-r",
            "remote-openocd",
            "--no-rebuild",
            check=False,
        )
        assert result.returncode != 0
        assert "ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION is required" in result.stdout

    def test_recording_rtt_reuses_thread_info_decision(self):
        result = self._west(
            "rtt",
            "-d",
            str(self.build_thread_info),
            "-r",
            "remote-openocd",
            "--no-rebuild",
            "--",
            "--rtt-address=0x20001000",
            extra_env={"ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION": "Open On-Chip Debugger 0.12.0"},
        )
        recording = self._recording(result.stdout)
        assert recording["thread_info"]["rtos_awareness"]
        assert (
            "$_TARGETNAME configure -rtos Zephyr"
            in recording["remote_session_request"]["process"]["argv"]
        )

    def test_recording_rtt_command_construction_without_io(self):
        cases = (
            ("rtt", ("--rtt-address=0x20001000", "--rtt-port=5566")),
            (
                "debug",
                ("--rtt-server", "--rtt-address=0x20001000", "--rtt-port=5566"),
            ),
            (
                "debugserver",
                ("--rtt-server", "--rtt-address=0x20001000", "--rtt-port=5566"),
            ),
        )
        for command, runner_args in cases:
            result = self._west(
                command,
                "-d",
                str(self.build_in_tree),
                "-r",
                "remote-openocd",
                "--no-rebuild",
                "--",
                *runner_args,
            )
            recording = self._recording(result.stdout)
            assert recording["rtt"]["address"] == 0x20001000
            assert recording["rtt"]["port"] == 5566
            assert recording["rtt"]["setup"] == (
                "batch-gdb" if command == "rtt" else "openocd-startup"
            )
            assert recording["rtt"]["launches_local_client"] == (command == "rtt")
            services = recording["remote_session_request"]["services"]
            assert any(item["name"] == "rtt" for item in services) == (command != "rtt")
            if command == "rtt":
                assert "--batch" in recording["local_gdb_argv"]
                assert "monitor rtt server start 5566 0" in recording["local_gdb_argv"]
            else:
                assert (
                    "rtt server start 5566 0"
                    in recording["remote_session_request"]["process"]["argv"]
                )

    def test_recording_direct_semihosting_commands_without_io(self):
        commands = (
            "init",
            "arm semihosting enable",
            "arm semihosting_fileio disable",
            "arm semihosting_redirect disable",
        )
        result = self._west(
            "debug",
            "-d",
            str(self.build_in_tree),
            "-r",
            "remote-openocd",
            "--no-rebuild",
            "--",
            "--no-load",
            "--no-init",
            *(f"--cmd-pre-init={command}" for command in commands),
            "--gdb-init=monitor reset run",
        )
        recording = self._recording(result.stdout)
        argv = recording["remote_session_request"]["process"]["argv"]
        for command in commands:
            assert command in argv
        assert [item["name"] for item in recording["remote_session_request"]["services"]] == [
            "gdb",
            "tcl",
            "telnet",
        ]
        assert not recording["rtt"]["enabled"]

    def test_explicit_elf_file_type_uses_zephyr_elf_flash_flow(self):
        result = self._west(
            "flash",
            "-d",
            str(self.build_in_tree),
            "-r",
            "remote-openocd",
            "--no-rebuild",
            "--",
            "--file-type=elf",
        )
        recording = self._recording(result.stdout)
        argv = recording["remote_session_request"]["process"]["argv"]
        assert any(argument.startswith("load_image ") for argument in argv)
        assert not any(argument.startswith("flash write_image ") for argument in argv)

    def test_config_change_regenerates_default_runner(self):
        """Regression coverage for prototype gate PG-009."""
        state = self._runner_state(self.build_in_tree)
        assert state["flash-runner"] == "openocd"
        self._write_config("remote")
        remote = self._west("flash", "-d", str(self.build_in_tree))
        assert "Re-running CMake" in remote.stdout
        assert "using runner remote-openocd" in remote.stdout
        assert self._runner_state(self.build_in_tree)["flash-runner"] == "remote-openocd"

        self._write_config("local")
        local = self._west("flash", "-d", str(self.build_in_tree))
        assert "Re-running CMake" in local.stdout
        assert "using runner openocd" in local.stdout
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
        with tempfile.TemporaryDirectory(prefix="zro-clean-install-") as directory:
            root = Path(directory)
            distribution = root / "zephyr-remote-openocd"
            home = root / "home"
            build = root / "build"
            cache = root / "cache"
            fake_openocd = root / "fake-openocd"
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

            clean_env = os.environ.copy()
            for name in (
                "PYTHONPATH",
                "ZEPHYR_REMOTE_OPENOCD_CONFIG",
                "ZEPHYR_REMOTE_OPENOCD_RECORD",
                "ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION",
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
                    "CCACHE_TEMPDIR": str(root / "ccache-tmp"),
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
            config = home / ".config" / "zephyr-remote-openocd" / "config.toml"
            assert (
                config.read_bytes()
                == (distribution / "resources" / "config.toml.example").read_bytes()
            )
            assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(config.stat().st_mode) == 0o600
            assert f"Configuration (created): {config}" in first_setup.stdout
            assert f"Module root: {distribution}" in first_setup.stdout
            assert "pyelftools: found" in first_setup.stdout
            assert str(ROOT) not in first_setup.stdout

            config.write_text(
                '[zephyr]\ndefault = "local"\n\n'
                '[remote]\nhost = "record-only"\nopenocd = "/remote/openocd"\n\n'
                '[ssh]\ncommand = ["ssh"]\n\n'
                '[[paths.map]]\nlocal = "/"\nremote = "/recorded"\n'
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
            assert "remote-openocd" in initial["runners"]
            assert initial["flash-runner"] == "openocd"
            local_context = west("flash", "-d", str(build), "--context")
            assert "openocd capabilities:" in local_context.stdout

            config.write_text(config.read_text().replace('default = "local"', 'default = "remote"'))
            clean_env["ZEPHYR_REMOTE_OPENOCD_RECORD"] = "1"
            recorded = west("flash", "-d", str(build))
            assert "Re-running CMake" in recorded.stdout
            assert "using runner remote-openocd" in recorded.stdout
            assert self._runner_state(build)["flash-runner"] == "remote-openocd"
            recording = self._recording(recorded.stdout)
            assert recording["command"] == "flash"
            assert recording["remote_session_request"]["host"] == "record-only"

    def test_zephyr44_adapter_respects_api_boundary(self):
        """Regression coverage for prototype gate PG-010."""
        adapter_path = ROOT / "python/zephyr_remote_openocd/zephyr44/runner.py"
        tree = ast.parse(adapter_path.read_text())
        adapter = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RemoteOpenOcdBinaryRunner"
        )
        methods = {node.name for node in adapter.body if isinstance(node, ast.FunctionDef)}
        assert {"name", "do_create", "do_run"}.issubset(methods)
        assert "capabilities" not in methods
        assert "do_add_parser" not in methods
        assert any(
            any(
                isinstance(base, ast.Name) and base.id == "OpenOcdBinaryRunner"
                for base in adapter.bases
            )
        )
        for node in ast.walk(adapter):
            if (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
            ):
                pytest.fail(f"private adapter dependency found: {node.attr}")

        imports = []
        for path in (ROOT / "python/zephyr_remote_openocd").rglob("*.py"):
            if "zephyr44" not in path.parts and "runners.openocd" in path.read_text():
                imports.append(path)
        assert imports == [], "OpenOcdBinaryRunner coupling escaped zephyr44"
        adapter_source = adapter_path.read_text()
        core_source = (self.zephyr_base / "scripts/west_commands/runners/core.py").read_text()
        openocd_source = (self.zephyr_base / "scripts/west_commands/runners/openocd.py").read_text()
        assert "self.run_client(" in adapter_source
        assert "def run_client(" in core_source
        assert "def run_client(" not in openocd_source
