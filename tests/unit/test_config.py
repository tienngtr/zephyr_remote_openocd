# SPDX-License-Identifier: Apache-2.0

import tempfile
import unittest
from pathlib import Path

from zephyr_remote_openocd.config import ConfigError, load_config, require_remote_settings

from tests.support import ROOT


class ConfigTests(unittest.TestCase):
    def load_text(self, text: str):
        directory = tempfile.TemporaryDirectory(dir=ROOT / ".scratch")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(text)
        return load_config(path)

    def test_defaults_when_file_is_absent(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            config = load_config(Path(directory) / "missing.toml")
        self.assertEqual(config.default, "local")
        self.assertEqual(config.ssh_command, ("ssh",))

    def test_canonical_template_matches_schema_defaults(self):
        config = load_config(ROOT / "resources" / "config.toml.example")
        self.assertEqual(config.default, "local")
        self.assertIsNone(config.remote_host)
        self.assertIsNone(config.remote_openocd)
        self.assertEqual(config.ssh_command, ("ssh",))
        self.assertEqual(config.forward_env, ())
        self.assertEqual(config.path_mappings, ())

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

    def test_unknown_keys_are_rejected(self):
        cases = (
            'future = true\n',
            '[zephyr]\ndefault = "local"\nfuture = true\n',
            '[remote]\nhost = "host"\nfuture = true\n',
            '[ssh]\ncommand = ["ssh"]\nfuture = true\n',
            '[openocd]\nforward_env = []\nfuture = true\n',
            '[paths]\nfuture = true\n',
            '[[paths.map]]\nlocal = "/a"\nremote = "/b"\nfuture = true\n',
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, "unknown"):
                self.load_text(text)

    def test_invalid_types_are_rejected(self):
        cases = (
            ('[zephyr]\ndefault = 1\n', "zephyr.default"),
            ('[remote]\nhost = 1\n', "remote.host"),
            ('[remote]\nopenocd = 1\n', "remote.openocd"),
            ('[ssh]\ncommand = "ssh"\n', "ssh.command"),
            ('[openocd]\nforward_env = "PROBE"\n', "forward_env"),
            ('[paths]\nmap = {}\n', "paths.map"),
        )
        for text, diagnostic in cases:
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, diagnostic):
                self.load_text(text)

    def test_mapping_requires_exactly_local_and_remote(self):
        with self.assertRaisesRegex(ConfigError, "missing remote"):
            self.load_text('[[paths.map]]\nlocal = "/local"\n')

    def test_empty_values_are_rejected(self):
        cases = (
            ('[remote]\nhost = ""\n', "remote.host"),
            ('[remote]\nopenocd = " "\n', "remote.openocd"),
            ('[ssh]\ncommand = []\n', "ssh.command"),
            ('[ssh]\ncommand = ["ssh", ""]\n', "ssh.command"),
            ('[openocd]\nforward_env = [""]\n', "forward_env"),
            ('[[paths.map]]\nlocal = ""\nremote = "/remote"\n', "local"),
            ('[[paths.map]]\nlocal = "/local"\nremote = ""\n', "remote"),
        )
        for text, diagnostic in cases:
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, diagnostic):
                self.load_text(text)

    def test_malformed_toml_is_actionable(self):
        with self.assertRaisesRegex(ConfigError, "invalid configuration.*config.toml"):
            self.load_text('[zephyr\ndefault = "local"\n')

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

    def test_duplicate_mapping_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "duplicate mappings"):
            self.load_text(
                '[[paths.map]]\nlocal = "/same"\nremote = "/one"\n'
                '[[paths.map]]\nlocal = "/same"\nremote = "/one"\n'
            )

    def test_paths_are_validated_and_local_paths_are_normalized(self):
        invalid = (
            ('[remote]\nopenocd = "openocd"\n', "remote.openocd"),
            ('[remote]\nopenocd = "/opt/../openocd"\n', "remote.openocd"),
            ('[remote]\nopenocd = "/opt//openocd"\n', "remote.openocd"),
            ('[[paths.map]]\nlocal = "relative"\nremote = "/remote"\n', "local"),
            ('[[paths.map]]\nlocal = "/local"\nremote = "relative"\n', "remote"),
            ('[[paths.map]]\nlocal = "/local"\nremote = "/remote/../other"\n', "remote"),
        )
        for text, diagnostic in invalid:
            with self.subTest(text=text), self.assertRaisesRegex(ConfigError, diagnostic):
                self.load_text(text)

        config = self.load_text('[[paths.map]]\nlocal = "/a/../local"\nremote = "/remote"\n')
        self.assertEqual(config.path_mappings[0].local, Path("/local"))

        config = self.load_text('[[paths.map]]\nlocal = "/local"\nremote = "/"\n')
        self.assertEqual(str(config.path_mappings[0].remote), "/")

    def test_forwarded_environment_names_must_be_unique(self):
        with self.assertRaisesRegex(ConfigError, "names must be unique"):
            self.load_text('[openocd]\nforward_env = ["PROBE", "PROBE"]\n')

    def test_remote_settings_are_required_only_for_remote_operations(self):
        config = self.load_text('[zephyr]\ndefault = "local"\n')
        with self.assertRaisesRegex(ConfigError, "remote.host is required.*config.toml"):
            require_remote_settings(config, "flash")

        config = self.load_text('[remote]\nhost = "openocd-host"\n')
        with self.assertRaisesRegex(ConfigError, "remote.openocd is required.*config.toml"):
            require_remote_settings(config, "debug")

        config = self.load_text(
            '[remote]\nhost = "openocd-host"\nopenocd = "/opt/openocd/bin/openocd"\n'
        )
        self.assertEqual(
            require_remote_settings(config, "flash"),
            ("openocd-host", "/opt/openocd/bin/openocd"),
        )


if __name__ == "__main__":
    unittest.main()
