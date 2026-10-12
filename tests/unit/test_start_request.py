# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from zephyr_remote_openocd.protocol import commands as protocol_commands


@pytest.fixture
def start_command():
    return {
        "version": 1,
        "type": "START",
        "completion_policy": "live_server",
        "argv": ["openocd", "{address}"],
        "environment": {"ZRO_TEST": "value"},
        "required_paths": [{"kind": "file", "path": "{workspace}/image"}],
        "services": [
            {"name": "gdb", "remote_port": 3333},
            {"name": "tcl", "remote_port": 6333},
        ],
        "required_output_sentinels": ["READY"],
        "readiness_timeout": 30.0,
        "literal_prefix": 1,
        "argv_templates": [],
        "preferred_address": None,
    }


def test_decode_start_accepts_full_output_lines_with_internal_spaces(start_command):
    start_command["required_output_sentinels"] = ["READY FOR START"]

    request = protocol_commands.decode_command(start_command)

    assert request.required_output_sentinels == ("READY FOR START",)


@pytest.mark.parametrize("address", (None, "127.64.0.7"))
def test_decode_start_accepts_address_preference(start_command, address):
    start_command["preferred_address"] = address
    assert protocol_commands.decode_command(start_command).preferred_address == address


def test_decode_start_requires_address_preference_field(start_command):
    del start_command["preferred_address"]
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)


@pytest.mark.parametrize(
    "address", (False, 1, [], "", "127.0.0.1", "127.64.0.0", "127.127.255.255", "::1")
)
def test_decode_start_rejects_invalid_address_preference(start_command, address):
    start_command["preferred_address"] = address
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)


def test_decode_command_rejects_malformed_required_path_before_launch(start_command):
    start_command["required_paths"] = [{"kind": "socket", "path": "not-valid"}]
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)


def test_decode_command_requires_explicit_argument_templates(start_command):
    del start_command["argv_templates"]
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)


@pytest.mark.parametrize(
    "templates",
    (
        None,
        [{"index": True, "parts": ["text"]}],
        [{"index": 0, "parts": ["cannot override configured prefix"]}],
        [{"index": 2, "parts": ["outside argv"]}],
        [{"index": 1, "parts": []}],
        [{"index": 1, "parts": ["nul\0"]}],
        [{"index": 1, "parts": [{"session": "unknown"}]}],
        [{"index": 1, "parts": [{"session": "address", "extra": True}]}],
        [{"index": 1, "parts": [{"tcl_word": [{"tcl_word": ["nested"]}]}]}],
        [{"index": 1, "parts": ["first"]}, {"index": 1, "parts": ["duplicate"]}],
    ),
)
def test_decode_command_rejects_malformed_owned_substitutions(start_command, templates):
    start_command["argv_templates"] = templates
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)


def test_decode_command_rejects_tcl_quoting_in_a_filesystem_path(start_command):
    start_command["required_paths"] = [
        {"kind": "file", "path": {"parts": [{"tcl_word": ["not a path template"]}]}}
    ]
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("version", 99, id="version-mismatch"),
        pytest.param("version", True, id="boolean-version"),
        pytest.param("argv", [], id="empty-argv"),
        pytest.param("environment", {"BAD=NAME": "value"}, id="environment-name"),
        pytest.param("required_output_sentinels", [" READY "], id="untrimmed-marker"),
        pytest.param("required_output_sentinels", ["READY", "READY"], id="duplicate-marker"),
        pytest.param("readiness_timeout", 0, id="zero-timeout"),
        pytest.param("literal_prefix", 3, id="prefix-outside-argv"),
    ),
)
def test_decode_command_rejects_invalid_start_values(start_command, field, value):
    start_command[field] = value
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)


@pytest.mark.parametrize(
    'listeners',
    (
        pytest.param(
            [
                {"name": "gdb", "remote_port": 3333},
                {"name": "gdb", "remote_port": 6333},
            ],
            id="duplicate-names",
        ),
        pytest.param(
            [
                {"name": "gdb", "remote_port": 3333},
                {"name": "tcl", "remote_port": 3333},
            ],
            id="duplicate-ports",
        ),
    ),
)
def test_decode_command_rejects_duplicate_listeners(start_command, listeners):
    start_command["services"] = listeners
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)


def test_decode_command_rejects_unknown_start_fields_and_unsupported_commands(start_command):
    start_command["future"] = True
    with pytest.raises(ValueError):
        protocol_commands.decode_command(start_command)
    with pytest.raises(ValueError):
        protocol_commands.decode_command({"version": 1, "type": "UNEXPECTED", "future": True})
