# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT

SETUP = ROOT / "scripts" / "setup.py"
TEMPLATE = ROOT / "resources" / "config.toml.example"


class SetupTests(unittest.TestCase):
    def run_setup(self, home: Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
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

    def test_creates_template_and_reports_activation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            home = Path(directory)
            result = self.run_setup(home, Path(directory).parent)
            config = home / ".config" / "zephyr-remote-openocd" / "config.toml"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_bytes(), TEMPLATE.read_bytes())
            self.assertIn(f"Configuration (created): {config}", result.stdout)
            self.assertIn(f"Module root: {ROOT}", result.stdout)
            self.assertIn("EXTRA_ZEPHYR_MODULES", result.stdout)
            self.assertEqual(stat.S_IMODE(config.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_existing_configuration_is_preserved_and_not_chmodded(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            home = Path(directory)
            config_dir = home / ".config" / "zephyr-remote-openocd"
            config_dir.mkdir(parents=True)
            config_parent = home / ".config"
            config = config_dir / "config.toml"
            config.write_text("[zephyr]\ndefault = \"remote\"\n")
            config_parent.chmod(0o755)
            config_dir.chmod(0o755)
            config.chmod(0o644)
            result = self.run_setup(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Configuration (already exists): {config}", result.stdout)
            self.assertEqual(config.read_text(), "[zephyr]\ndefault = \"remote\"\n")
            self.assertEqual(stat.S_IMODE(config_parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)

    def test_existing_non_file_configuration_fails_actionably(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            home = Path(directory)
            config = home / ".config" / "zephyr-remote-openocd" / "config.toml"
            config.mkdir(parents=True)
            result = self.run_setup(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a regular file", result.stderr)


class DependencyDiagnosticTests(unittest.TestCase):
    @staticmethod
    def load_setup_module():
        import importlib.util

        spec = importlib.util.spec_from_file_location("zro_setup", SETUP)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_dependency_detection_found_and_missing(self):
        setup = self.load_setup_module()
        self.assertTrue(setup.pyelftools_available(lambda _: object()))
        self.assertFalse(setup.pyelftools_available(lambda _: None))

    def test_dependency_status_messages(self):
        setup = self.load_setup_module()
        found = io.StringIO()
        with contextlib.redirect_stdout(found):
            setup._print_dependency_status(lambda _: object())
        self.assertIn("pyelftools: found", found.getvalue())

        missing = io.StringIO()
        with contextlib.redirect_stdout(missing):
            setup._print_dependency_status(lambda _: None)
        self.assertIn("Warning: pyelftools is not available", missing.getvalue())
        self.assertIn("Use the Python environment configured for Zephyr", missing.getvalue())


if __name__ == "__main__":
    unittest.main()
