# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest
from zephyr_remote_openocd.config import (
    ConfigError,
    default_config_path,
    load_config,
    resolve_remote,
)

from tests.support import ROOT


def load_text(tmp_path: Path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return load_config(path)


def test_defaults_when_file_is_absent(tmp_path: Path):
    config = load_config(tmp_path / "missing.yaml")
    assert config.default_runner == "openocd"
    assert config.presets == {}
    assert config.remotes == {}


def test_default_path_uses_yaml_and_override(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", raising=False)
    assert default_config_path().name == "config.yaml"
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", "~/custom.conf")
    assert default_config_path() == Path.home() / "custom.conf"


def test_empty_document_is_empty_mapping(tmp_path: Path):
    config = load_text(tmp_path, "\n# no settings\n")
    assert config.default_runner == "openocd"


def test_canonical_template_loads():
    config = load_config(ROOT / "resources" / "config.yaml.example")
    assert config.default_runner == "openocd"
    assert "default" in config.presets


@pytest.mark.parametrize("runner", ("openocd", "remote_openocd"))
def test_supported_default_runner_values(tmp_path: Path, runner: str):
    config = load_text(tmp_path, f"default_runner: {runner}\n")
    assert config.default_runner == runner


@pytest.mark.parametrize(
    "text",
    (
        "future: true\n",
        "default_runner: elsewhere\n",
        "presets:\n  p:\n    future: true\n",
        "remotes:\n  r:\n    future: true\n",
        "presets:\n  p: null\n",
    ),
)
def test_unknown_and_invalid_values_are_rejected(tmp_path: Path, text: str):
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_text(tmp_path, text)


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="duplicate"):
        load_text(tmp_path, "default_runner: openocd\ndefault_runner: openocd\n")


def test_multiple_documents_are_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="one document"):
        load_text(tmp_path, "default_runner: openocd\n---\ndefault_runner: openocd\n")


@pytest.mark.parametrize(
    "text",
    (
        "default_runner: null\n",
        "default_remote: null\n",
        "remotes:\n  r:\n    ssh_host: true\n",
        "remotes:\n  r:\n    openocd_command: [openocd, null]\n",
        "remotes:\n  r:\n    forward_env: [A, A]\n",
    ),
)
def test_null_and_wrong_types_are_rejected(tmp_path: Path, text: str):
    with pytest.raises(ConfigError):
        load_text(tmp_path, text)


def test_command_arguments_and_preset_resolution(tmp_path: Path):
    config = load_text(
        tmp_path,
        """default_remote: lab
presets:
  base:
    openocd_command: [~/bin/openocd, --debug, '']
    ssh_command: [ssh, -F, /a file]
    forward_env: [PROBE]
    path_mappings: {'/tmp': '~/remote'}
remotes:
  lab:
    preset: base
    ssh_host: machine
""",
    )
    selected = resolve_remote(config)
    assert selected.ssh_host == "machine"
    assert selected.openocd_command == ("~/bin/openocd", "--debug", "")
    assert selected.ssh_command == ("ssh", "-F", "/a file")
    assert str(selected.path_mappings[0].remote) == "~/remote"


def test_remote_overrides_replace_whole_settings(tmp_path: Path):
    config = load_text(
        tmp_path,
        """default_remote: r
presets:
  p:
    openocd_command: [openocd]
    forward_env: [A, B]
remotes:
  r:
    preset: p
    openocd_command: [custom]
    forward_env: []
""",
    )
    selected = resolve_remote(config)
    assert selected.openocd_command == ("custom",)
    assert selected.forward_env == ()


def test_selected_remote_and_preset_errors_are_deferred(tmp_path: Path):
    config = load_text(tmp_path, "remotes:\n  r:\n    preset: missing\n")
    with pytest.raises(ConfigError, match="missing preset"):
        resolve_remote(config, "r")
    with pytest.raises(ConfigError, match="does not exist"):
        resolve_remote(config, "nope")


def test_selection_precedence_and_empty_environment(tmp_path: Path, monkeypatch):
    config = load_text(
        tmp_path,
        """default_remote: default
remotes:
  default: {openocd_command: [default]}
  env: {openocd_command: [env]}
  cli: {openocd_command: [cli]}
""",
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "env")
    assert resolve_remote(config).name == "env"
    assert resolve_remote(config, "cli").name == "cli"
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "")
    assert resolve_remote(config).name == "default"


def test_no_remote_is_actionable(tmp_path: Path):
    config = load_text(tmp_path, "default_runner: openocd\n")
    with pytest.raises(ConfigError, match="no remote selected"):
        resolve_remote(config)


def test_local_mapping_duplicates_are_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="mappings"):
        load_text(
            tmp_path,
            """remotes:
  r:
    path_mappings:
      /tmp/../tmp: /one
      /tmp: /one
""",
        )
