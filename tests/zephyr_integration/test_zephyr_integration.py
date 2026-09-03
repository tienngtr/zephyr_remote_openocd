from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests.support import ROOT, env_path

try:
    import yaml
except ImportError:  # pragma: no cover - handled as an integration prerequisite
    yaml = None


class ZephyrIntegrationTests(unittest.TestCase):
    """Permanent coverage for retired prototype gates PG-001 through PG-010."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.zephyr_base = env_path("ZEPHYR_BASE")
        cls.openocd_board = os.environ.get("OPENOCD_TEST_BOARD")
        cls.no_openocd_board = os.environ.get("NON_OPENOCD_TEST_BOARD", "native_sim/native/64")
        cls.west = env_path("WEST") or (Path(shutil.which("west")) if shutil.which("west") else None)
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
            raise unittest.SkipTest("Zephyr integration prerequisites missing: " + ", ".join(missing))

        cls._scratch = tempfile.TemporaryDirectory(prefix="zephyr-integration-", dir=ROOT / ".scratch")
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
            "build", "-b", cls.openocd_board, str(sample), "-d", str(cls.build_in_tree),
            "--", f"-DUSER_CACHE_DIR={cls.cache}", f"-DOPENOCD={cls.fake_openocd}",
        )
        cls._west(
            "build", "-b", cls.openocd_board, str(sample), "-d", str(cls.build_thread_info),
            "--", f"-DUSER_CACHE_DIR={cls.cache}", f"-DOPENOCD={cls.fake_openocd}",
            "-DCONFIG_DEBUG_THREAD_INFO=y",
        )
        shutil.copytree(sample, cls.app_out_tree)
        cls._west(
            "build", "-b", cls.openocd_board, str(cls.app_out_tree), "-d", str(cls.build_out_tree),
            "--", f"-DUSER_CACHE_DIR={cls.cache}", f"-DOPENOCD={cls.fake_openocd}",
        )
        cls._west(
            "build", "--cmake-only", "-b", cls.no_openocd_board, str(sample),
            "-d", str(cls.build_without_openocd), "--", f"-DUSER_CACHE_DIR={cls.cache}",
        )

    @classmethod
    def tearDownClass(cls):
        scratch = getattr(cls, "_scratch", None)
        if scratch is not None:
            scratch.cleanup()
        super().tearDownClass()

    @classmethod
    def _write_config(cls, selected: str):
        cls.config.write_text(
            f'[zephyr]\ndefault = "{selected}"\n\n'
            '[remote]\nhost = "record-only"\nopenocd = "/remote/openocd"\n\n'
            '[ssh]\ncommand = ["ssh"]\n\n'
            '[[paths.map]]\nlocal = "/"\nremote = "/recorded"\n'
        )

    @classmethod
    def _west(cls, *args: str, check: bool = True, extra_env=None):
        env = os.environ.copy()
        env.update({
            "EXTRA_ZEPHYR_MODULES": str(ROOT),
            "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(cls.config),
            "ZEPHYR_REMOTE_OPENOCD_RECORD": "1",
            "CCACHE_DIR": str(cls.ccache),
            "CCACHE_TEMPDIR": str(cls.ccache_tmp),
        })
        env.update(extra_env or {})
        result = subprocess.run(
            [str(cls.west), *args], cwd=cls.zephyr_base.parent, env=env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False, timeout=180,
        )
        if check and result.returncode:
            raise AssertionError(f"west {' '.join(args)} failed ({result.returncode}):\n{result.stdout}")
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
        self.assertIn(str(ROOT), modules)
        self.assertTrue((self.build_in_tree / "zephyr" / "zephyr.elf").is_file())

    def test_out_of_tree_application_build(self):
        """Regression coverage for prototype gate PG-003."""
        self.assertFalse(str(self.app_out_tree).startswith(str(self.zephyr_base)))
        self.assertTrue((self.build_out_tree / "zephyr" / "zephyr.elf").is_file())

    def test_runner_registration_is_conditional_and_non_destructive(self):
        """Regression coverage for prototype gates PG-004 and PG-007."""
        enabled = self._runner_state(self.build_in_tree)["runners"]
        disabled = self._runner_state(self.build_without_openocd)["runners"]
        self.assertEqual(enabled.count("remote-openocd"), 1)
        self.assertIn("openocd", enabled)
        self.assertNotIn("remote-openocd", disabled)
        context = self._west(
            "flash", "-d", str(self.build_in_tree), "-r", "openocd", "--context"
        )
        self.assertIn("openocd capabilities:", context.stdout)

    def test_openocd_arguments_are_mirrored_exactly(self):
        """Regression coverage for prototype gate PG-005."""
        args = self._runner_state(self.build_in_tree)["args"]
        self.assertEqual(args["remote-openocd"], args["openocd"])

    def test_recording_commands_receive_runner_config_without_io(self):
        """Regression coverage for prototype gates PG-006 and PG-008."""
        for command in ("flash", "debug", "attach", "debugserver"):
            with self.subTest(command=command):
                result = self._west(
                    command, "-d", str(self.build_in_tree), "-r", "remote-openocd",
                    "--no-rebuild",
                )
                recording = self._recording(result.stdout)
                self.assertEqual(recording["command"], command)
                config = recording["runner_config"]
                for required in ("board_dir", "elf_file", "gdb", "openocd"):
                    self.assertTrue(config[required], required)
                for optional in ("hex_file", "bin_file", "openocd_search"):
                    self.assertIn(optional, config)
                if command == "flash":
                    request = recording["remote_session_request"]
                    self.assertEqual(request["host"], "record-only")
                    argv = request["process"]["argv"]
                    self.assertEqual(argv[0], "/remote/openocd")
                    self.assertIn("bindto {address}", argv)
                    self.assertFalse(any(" disabled" in argument for argument in argv))
                    if recording["runner_args"].get("file_type") == "elf":
                        self.assertTrue(any(argument.startswith("load_image ") for argument in argv))
                    else:
                        self.assertTrue(any(argument.startswith("flash write_image ") for argument in argv))
                    self.assertEqual(request["process"]["environment"], [])
                else:
                    request = recording["remote_session_request"]
                    self.assertEqual(
                        [(item["name"], item["local_port"], item["remote_port"]) for item in request["services"]],
                        [("gdb", 3333, 3333), ("tcl", 6333, 6333), ("telnet", 4444, 4444)],
                    )
                    self.assertRegex(request["process"]["readiness_marker"], r"^ZRO_READY_[0-9a-f]{32}$")
                    if command == "debugserver":
                        self.assertIsNone(recording["local_gdb_argv"])
                        self.assertIn("reset init", request["process"]["argv"])
                    else:
                        client = recording["local_gdb_argv"]
                        self.assertIn("target extended-remote 127.0.0.1:3333", client)
                        self.assertEqual("load" in client, command == "debug")

    def test_recording_thread_info_uses_injected_remote_version(self):
        result = self._west(
            "debug", "-d", str(self.build_thread_info), "-r", "remote-openocd",
            "--no-rebuild", extra_env={
                "ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION": "Open On-Chip Debugger 0.12.0"
            },
        )
        recording = self._recording(result.stdout)
        self.assertEqual(recording["thread_info"], {
            "requested": True,
            "version": "Open On-Chip Debugger 0.12.0",
            "version_source": "injected",
            "rtos_awareness": True,
        })
        self.assertIn(
            "$_TARGETNAME configure -rtos Zephyr",
            recording["remote_session_request"]["process"]["argv"],
        )

    def test_recording_thread_info_requires_injected_version(self):
        result = self._west(
            "debug", "-d", str(self.build_thread_info), "-r", "remote-openocd",
            "--no-rebuild", check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ZEPHYR_REMOTE_OPENOCD_RECORD_VERSION is required", result.stdout)

    def test_rtt_operations_remain_explicitly_unsupported(self):
        for command in (
            ("rtt",),
            ("debug", "--", "--rtt-server"),
            ("debugserver", "--", "--rtt-server"),
        ):
            with self.subTest(command=command):
                result = self._west(
                    command[0], "-d", str(self.build_in_tree), "-r", "remote-openocd",
                    "--no-rebuild", *command[1:], check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("RTT support is not implemented", result.stdout)

    def test_explicit_elf_file_type_uses_zephyr_elf_flash_flow(self):
        result = self._west(
            "flash", "-d", str(self.build_in_tree), "-r", "remote-openocd",
            "--no-rebuild", "--", "--file-type=elf",
        )
        recording = self._recording(result.stdout)
        argv = recording["remote_session_request"]["process"]["argv"]
        self.assertTrue(any(argument.startswith("load_image ") for argument in argv))
        self.assertFalse(any(argument.startswith("flash write_image ") for argument in argv))

    def test_config_change_regenerates_default_runner(self):
        """Regression coverage for prototype gate PG-009."""
        state = self._runner_state(self.build_in_tree)
        self.assertEqual(state["flash-runner"], "openocd")
        self._write_config("remote")
        remote = self._west("flash", "-d", str(self.build_in_tree))
        self.assertIn("Re-running CMake", remote.stdout)
        self.assertIn("using runner remote-openocd", remote.stdout)
        self.assertEqual(self._runner_state(self.build_in_tree)["flash-runner"], "remote-openocd")

        self._write_config("local")
        local = self._west("flash", "-d", str(self.build_in_tree))
        self.assertIn("Re-running CMake", local.stdout)
        self.assertIn("using runner openocd", local.stdout)
        self.assertEqual(self._runner_state(self.build_in_tree)["flash-runner"], "openocd")

    def test_zephyr44_adapter_respects_api_boundary(self):
        """Regression coverage for prototype gate PG-010."""
        adapter_path = ROOT / "python/zephyr_remote_openocd/zephyr44/runner.py"
        tree = ast.parse(adapter_path.read_text())
        adapter = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RemoteOpenOcdBinaryRunner"
        )
        methods = {node.name for node in adapter.body if isinstance(node, ast.FunctionDef)}
        self.assertTrue({"name", "do_create", "do_run"}.issubset(methods))
        self.assertNotIn("capabilities", methods)
        self.assertNotIn("do_add_parser", methods)
        self.assertTrue(
            any(isinstance(base, ast.Name) and base.id == "OpenOcdBinaryRunner" for base in adapter.bases)
        )
        for node in ast.walk(adapter):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__"):
                self.fail(f"private adapter dependency found: {node.attr}")

        imports = []
        for path in (ROOT / "python/zephyr_remote_openocd").rglob("*.py"):
            if "zephyr44" not in path.parts and "runners.openocd" in path.read_text():
                imports.append(path)
        self.assertEqual(imports, [], "OpenOcdBinaryRunner coupling escaped zephyr44")
        adapter_source = adapter_path.read_text()
        core_source = (self.zephyr_base / "scripts/west_commands/runners/core.py").read_text()
        openocd_source = (self.zephyr_base / "scripts/west_commands/runners/openocd.py").read_text()
        self.assertIn("self.run_client(", adapter_source)
        self.assertIn("def run_client(", core_source)
        self.assertNotIn("def run_client(", openocd_source)


if __name__ == "__main__":
    unittest.main()
