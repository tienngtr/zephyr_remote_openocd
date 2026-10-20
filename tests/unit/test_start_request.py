# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest
from zephyr_remote_openocd.protocol import commands as protocol_commands
from zephyr_remote_openocd.protocol.commands import StartRequest


@pytest.fixture
def start_command() -> dict[str, object]:
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


def _decode(command: dict[str, object]) -> StartRequest:
    return protocol_commands.decode_command(json.dumps(command).encode() + b"\n")


def testdecode_command_requires_address_preference_field(start_command: dict[str, object]) -> None:
    del start_command["preferred_address"]
    with pytest.raises(ValueError):
        _decode(start_command)


@pytest.mark.parametrize(
    "address", (False, 1, [], "", "127.0.0.1", "127.64.0.0", "127.127.255.255", "::1")
)
def testdecode_command_rejects_invalid_address_preference(
    start_command: dict[str, object], address: object
) -> None:
    start_command["preferred_address"] = address
    with pytest.raises(ValueError):
        _decode(start_command)


@pytest.mark.parametrize(
    "path",
    (
        pytest.param({"kind": "socket", "path": "not-valid"}, id="unsupported-kind"),
        pytest.param({"kind": "file", "path": ""}, id="empty-path"),
        pytest.param({"kind": "file", "path": "bad\0path"}, id="nul-path"),
        pytest.param({"kind": "file", "path": "image", "future": True}, id="unknown-field"),
        pytest.param(
            {"kind": "file", "path": {"parts": ["image"], "future": True}},
            id="unknown-template-field",
        ),
    ),
)
def test_decode_command_rejects_malformed_required_path_before_launch(
    start_command: dict[str, object], path: dict[str, object]
) -> None:
    start_command["required_paths"] = [path]
    with pytest.raises(ValueError):
        _decode(start_command)


def test_decode_command_requires_explicit_argument_templates(
    start_command: dict[str, object],
) -> None:
    del start_command["argv_templates"]
    with pytest.raises(ValueError):
        _decode(start_command)


@pytest.mark.parametrize(
    "templates",
    (
        None,
        [{"index": True, "parts": ["text"]}],
        [{"index": 0, "parts": ["cannot override configured prefix"]}],
        [{"index": 2, "parts": ["outside argv"]}],
        [{"index": 1, "parts": []}],
        pytest.param([{"index": 1, "parts": "literal text"}], id="non-list-parts"),
        pytest.param(
            [{"index": 1, "parts": ["text"], "future": True}], id="unknown-template-field"
        ),
        [{"index": 1, "parts": ["nul\0"]}],
        [{"index": 1, "parts": [{"session": "unknown"}]}],
        [{"index": 1, "parts": [{"session": "address", "extra": True}]}],
        [{"index": 1, "parts": [{"tcl_word": [{"tcl_word": ["nested"]}]}]}],
        [{"index": 1, "parts": ["first"]}, {"index": 1, "parts": ["duplicate"]}],
    ),
)
def test_decode_command_rejects_malformed_owned_substitutions(
    start_command: dict[str, object], templates: object
) -> None:
    start_command["argv_templates"] = templates
    with pytest.raises(ValueError):
        _decode(start_command)


def test_decode_command_rejects_tcl_quoting_in_a_filesystem_path(
    start_command: dict[str, object],
) -> None:
    start_command["required_paths"] = [
        {"kind": "file", "path": {"parts": [{"tcl_word": ["not a path template"]}]}}
    ]
    with pytest.raises(ValueError):
        _decode(start_command)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("argv", [], id="empty-argv"),
        pytest.param("argv", ["", "argument"], id="empty-program"),
        pytest.param("argv", ["openocd", "bad\0argument"], id="nul-in-argv"),
        pytest.param("environment", {"BAD=NAME": "value"}, id="environment-name"),
        pytest.param("environment", {"": "value"}, id="empty-environment-name"),
        pytest.param("environment", {"BAD\0NAME": "value"}, id="nul-in-environment-name"),
        pytest.param("environment", {"KEY": ["value"]}, id="non-string-environment-value"),
        pytest.param("environment", {"KEY": "bad\0value"}, id="nul-in-environment-value"),
        pytest.param(
            "services",
            [{"name": "gdb", "remote_port": 3333, "future": True}],
            id="unknown-service-field",
        ),
        pytest.param("services", [{"name": "", "remote_port": 3333}], id="empty-service-name"),
        pytest.param("services", [{"name": 1, "remote_port": 3333}], id="non-string-service-name"),
        pytest.param("services", [{"name": "gdb", "remote_port": 0}], id="zero-service-port"),
        pytest.param(
            "services", [{"name": "gdb", "remote_port": 65536}], id="out-of-range-service-port"
        ),
        pytest.param("services", [{"name": "gdb", "remote_port": True}], id="boolean-service-port"),
        pytest.param("services", [{"name": "gdb", "remote_port": 3333.0}], id="float-service-port"),
        pytest.param("required_output_sentinels", "READY", id="non-list-markers"),
        pytest.param("required_output_sentinels", [""], id="empty-marker"),
        pytest.param("required_output_sentinels", [" READY "], id="untrimmed-marker"),
        pytest.param("required_output_sentinels", ["READY", "READY"], id="duplicate-marker"),
        pytest.param("required_output_sentinels", ["RE\0ADY"], id="nul-in-marker"),
        pytest.param("required_output_sentinels", ["RE\nADY"], id="newline-in-marker"),
        pytest.param("required_output_sentinels", ["RE\rADY"], id="carriage-return-in-marker"),
        pytest.param("readiness_timeout", 0, id="zero-timeout"),
        pytest.param("readiness_timeout", True, id="boolean-timeout"),
        pytest.param("literal_prefix", 3, id="prefix-outside-argv"),
        pytest.param("literal_prefix", True, id="boolean-prefix"),
        pytest.param("literal_prefix", 1.0, id="float-prefix"),
    ),
)
def test_decode_command_rejects_invalid_start_values(
    start_command: dict[str, object], field: str, value: object
) -> None:
    start_command[field] = value
    with pytest.raises(ValueError):
        _decode(start_command)


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
def test_decode_command_rejects_duplicate_listeners(
    start_command: dict[str, object], listeners: list[dict[str, object]]
) -> None:
    start_command["services"] = listeners
    with pytest.raises(ValueError):
        _decode(start_command)


def test_decode_command_rejects_unknown_start_fields_and_unsupported_commands(
    start_command: dict[str, object],
) -> None:
    start_command["future"] = True
    with pytest.raises(ValueError):
        _decode(start_command)
    with pytest.raises(ValueError):
        _decode({"version": 1, "type": "UNEXPECTED", "future": True})
