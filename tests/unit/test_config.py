# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest
from zephyr_remote_openocd.config import ConfigError, load_config, require_remote_settings

from tests.support import ROOT


def load_text(tmp_path: Path, text: str):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return load_config(path)


def test_defaults_when_file_is_absent(tmp_path: Path):
    config = load_config(tmp_path / "missing.toml")
    assert config.default == "local"
    assert config.ssh_command == ("ssh",)


def test_canonical_template_matches_schema_defaults():
    config = load_config(ROOT / "resources" / "config.toml.example")
    assert config.default == "local"
    assert config.remote_host is None
    assert config.remote_openocd is None
    assert config.ssh_command == ("ssh",)
    assert config.forward_env == ()
    assert config.path_mappings == ()


def test_fixed_ssh_arguments(tmp_path: Path):
    config = load_text(tmp_path, '[ssh]\ncommand = ["ssh", "-F", "/a file"]\n')
    assert config.ssh_command == ("ssh", "-F", "/a file")


def test_bad_default_is_actionable(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[zephyr]\ndefault = "elsewhere"\n')
    with pytest.raises(ConfigError, match=str(path)):
        load_config(path)


@pytest.mark.parametrize(
    "text",
    (
        "future = true\n",
        '[zephyr]\ndefault = "local"\nfuture = true\n',
        '[remote]\nhost = "host"\nfuture = true\n',
        '[ssh]\ncommand = ["ssh"]\nfuture = true\n',
        '[openocd]\nforward_env = []\nfuture = true\n',
        "[paths]\nfuture = true\n",
        '[[paths.map]]\nlocal = "/a"\nremote = "/b"\nfuture = true\n',
    ),
)
def test_unknown_keys_are_rejected(tmp_path: Path, text: str):
    with pytest.raises(ConfigError, match="unknown"):
        load_text(tmp_path, text)


@pytest.mark.parametrize(
    ("text", "diagnostic"),
    (
        ("[zephyr]\ndefault = 1\n", "zephyr.default"),
        ("[remote]\nhost = 1\n", "remote.host"),
        ("[remote]\nopenocd = 1\n", "remote.openocd"),
        ('[ssh]\ncommand = "ssh"\n', "ssh.command"),
        ('[openocd]\nforward_env = "PROBE"\n', "forward_env"),
        ("[paths]\nmap = {}\n", "paths.map"),
    ),
)
def test_invalid_types_are_rejected(tmp_path: Path, text: str, diagnostic: str):
    with pytest.raises(ConfigError, match=diagnostic):
        load_text(tmp_path, text)


def test_mapping_requires_exactly_local_and_remote(tmp_path: Path):
    with pytest.raises(ConfigError, match="missing remote"):
        load_text(tmp_path, '[[paths.map]]\nlocal = "/local"\n')


@pytest.mark.parametrize(
    ("text", "diagnostic"),
    (
        ('[remote]\nhost = ""\n', "remote.host"),
        ('[remote]\nopenocd = " "\n', "remote.openocd"),
        ('[ssh]\ncommand = []\n', "ssh.command"),
        ('[ssh]\ncommand = ["ssh", ""]\n', "ssh.command"),
        ('[openocd]\nforward_env = [""]\n', "forward_env"),
        ('[[paths.map]]\nlocal = ""\nremote = "/remote"\n', "local"),
        ('[[paths.map]]\nlocal = "/local"\nremote = ""\n', "remote"),
    ),
)
def test_empty_values_are_rejected(tmp_path: Path, text: str, diagnostic: str):
    with pytest.raises(ConfigError, match=diagnostic):
        load_text(tmp_path, text)


def test_malformed_toml_is_actionable(tmp_path: Path):
    with pytest.raises(ConfigError, match="invalid configuration.*config.toml"):
        load_text(tmp_path, "[zephyr\ndefault = \"local\"\n")


def test_environment_and_path_mappings(tmp_path: Path):
    config = load_text(
        tmp_path,
        '[openocd]\nforward_env = ["PROBE"]\n'
        '[[paths.map]]\nlocal = "/opt/tree"\nremote = "/remote/tree"\n'
        '[[paths.map]]\nlocal = "/opt/tree/specific"\nremote = "/special"\n',
    )
    assert config.forward_env == ("PROBE",)
    assert len(config.path_mappings) == 2


def test_conflicting_mapping_is_actionable(tmp_path: Path):
    with pytest.raises(ConfigError, match="conflicting mappings"):
        load_text(
            tmp_path,
            '[[paths.map]]\nlocal = "/same"\nremote = "/one"\n'
            '[[paths.map]]\nlocal = "/same"\nremote = "/two"\n',
        )


def test_duplicate_mapping_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="duplicate mappings"):
        load_text(
            tmp_path,
            '[[paths.map]]\nlocal = "/same"\nremote = "/one"\n'
            '[[paths.map]]\nlocal = "/same"\nremote = "/one"\n',
        )


def test_paths_are_validated_and_local_paths_are_normalized(tmp_path: Path):
    invalid = (
        ('[remote]\nopenocd = "openocd"\n', "remote.openocd"),
        ('[remote]\nopenocd = "/opt/../openocd"\n', "remote.openocd"),
        ('[remote]\nopenocd = "/opt//openocd"\n', "remote.openocd"),
        ('[[paths.map]]\nlocal = "relative"\nremote = "/remote"\n', "local"),
        ('[[paths.map]]\nlocal = "/local"\nremote = "relative"\n', "remote"),
        ('[[paths.map]]\nlocal = "/local"\nremote = "/remote/../other"\n', "remote"),
    )
    for text, diagnostic in invalid:
        with pytest.raises(ConfigError, match=diagnostic):
            load_text(tmp_path, text)

    config = load_text(tmp_path, '[[paths.map]]\nlocal = "/a/../local"\nremote = "/remote"\n')
    assert config.path_mappings[0].local == Path("/local")
    config = load_text(tmp_path, '[[paths.map]]\nlocal = "/local"\nremote = "/"\n')
    assert str(config.path_mappings[0].remote) == "/"


def test_forwarded_environment_names_must_be_unique(tmp_path: Path):
    with pytest.raises(ConfigError, match="names must be unique"):
        load_text(tmp_path, '[openocd]\nforward_env = ["PROBE", "PROBE"]\n')


def test_remote_settings_are_required_only_for_remote_operations(tmp_path: Path):
    config = load_text(tmp_path, '[zephyr]\ndefault = "local"\n')
    with pytest.raises(ConfigError, match="remote.host is required.*config.toml"):
        require_remote_settings(config, "flash")
    config = load_text(tmp_path, '[remote]\nhost = "openocd-host"\n')
    with pytest.raises(ConfigError, match="remote.openocd is required.*config.toml"):
        require_remote_settings(config, "debug")
    config = load_text(
        tmp_path,
        '[remote]\nhost = "openocd-host"\nopenocd = "/opt/openocd/bin/openocd"\n',
    )
    assert require_remote_settings(config, "flash") == (
        "openocd-host",
        "/opt/openocd/bin/openocd",
    )
