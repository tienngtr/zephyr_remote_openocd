from pathlib import Path
import tempfile
import unittest

from tests.support import ROOT
from zephyr_remote_openocd.config import ConfigError, load_config


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


if __name__ == "__main__":
    unittest.main()

