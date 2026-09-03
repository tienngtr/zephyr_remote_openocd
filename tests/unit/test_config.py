# SPDX-License-Identifier: Apache-2.0

import tempfile
import unittest
from pathlib import Path

from zephyr_remote_openocd.config import ConfigError, load_config

from tests.support import ROOT


class ConfigTests(unittest.TestCase):
    def test_defaults_when_file_is_absent(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            config = load_config(Path(directory) / "missing.toml")
        self.assertEqual(config.default, "local")
        self.assertEqual(config.ssh_command, ("ssh",))

    def test_fixed_ssh_arguments(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[ssh]\ncommand = ["ssh", "-F", "/a file"]\n')
            config = load_config(path)
        self.assertEqual(config.ssh_command, ("ssh", "-F", "/a file"))

    def test_bad_default_is_actionable(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[zephyr]\ndefault = "elsewhere"\n')
            with self.assertRaisesRegex(ConfigError, str(path)):
                load_config(path)

    def test_environment_and_path_mappings(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[openocd]\nforward_env = ["PROBE"]\n'
                '[[paths.map]]\nlocal = "/opt/tree"\nremote = "/remote/tree"\n'
                '[[paths.map]]\nlocal = "/opt/tree/specific"\nremote = "/special"\n'
            )
            config = load_config(path)
        self.assertEqual(config.forward_env, ("PROBE",))
        self.assertEqual(len(config.path_mappings), 2)

    def test_conflicting_mapping_is_actionable(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[[paths.map]]\nlocal = "/same"\nremote = "/one"\n'
                '[[paths.map]]\nlocal = "/same"\nremote = "/two"\n'
            )
            with self.assertRaisesRegex(ConfigError, "conflicting mappings"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
