# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest
from zephyr_remote_openocd.protocol import commands as protocol_commands
from zephyr_remote_openocd.protocol.arguments import materialize_argv
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
        "readiness_evidence": ["startup"],
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


LOG_SPOOL_WORD = {"tcl_word": [{"session": "log_spool"}]}


def test_log_spool_in_its_one_form_requests_the_log_channel(
    start_command: dict[str, object],
) -> None:
    start_command["argv"] = ["openocd", "--log_output", "{log_spool}"]
    start_command["literal_prefix"] = 2
    start_command["argv_templates"] = [{"index": 2, "parts": [LOG_SPOOL_WORD]}]
    request = _decode(start_command)
    assert request.uses_log_spool
    assert materialize_argv(
        request.argv,
        workspace="/w",
        address="127.64.0.7",
        literal_prefix=request.literal_prefix,
        argv_templates=request.argv_templates,
        log_spool="/w/$log [x]",
    ) == ("openocd", "--log_output", '"/w/\\$log \\[x\\]"')


def test_start_without_log_spool_requests_no_log_channel(
    start_command: dict[str, object],
) -> None:
    assert not _decode(start_command).uses_log_spool


@pytest.mark.parametrize(
    ("templates", "required_path"),
    (
        pytest.param([{"index": 1, "parts": [{"session": "log_spool"}]}], None, id="plain-part"),
        pytest.param(
            [{"index": 1, "parts": [{"tcl_word": [{"session": "log_spool"}, ".log"]}]}],
            None,
            id="tcl-word-suffix",
        ),
        pytest.param(
            [{"index": 1, "parts": [{"tcl_word": ["/", {"session": "log_spool"}]}]}],
            None,
            id="tcl-word-prefix",
        ),
        pytest.param(
            [{"index": 1, "parts": ["log_output ", LOG_SPOOL_WORD]}], None, id="template-prefix"
        ),
        pytest.param(
            [{"index": 1, "parts": [LOG_SPOOL_WORD, LOG_SPOOL_WORD]}], None, id="repeated-word"
        ),
        pytest.param(
            [{"index": 1, "parts": [LOG_SPOOL_WORD]}, {"index": 2, "parts": [LOG_SPOOL_WORD]}],
            None,
            id="second-template",
        ),
        pytest.param(
            [], {"kind": "file", "path": {"parts": [{"session": "log_spool"}]}}, id="required-path"
        ),
    ),
)
def test_log_spool_outside_its_one_form_is_rejected_before_startup(
    start_command: dict[str, object],
    templates: list[object],
    required_path: dict[str, object] | None,
) -> None:
    start_command["argv"] = ["openocd", "first", "second"]
    start_command["argv_templates"] = templates
    if required_path is not None:
        start_command["required_paths"] = [required_path]
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
        pytest.param("readiness_evidence", "startup", id="non-list-evidence"),
        pytest.param("readiness_evidence", [""], id="empty-evidence"),
        pytest.param("readiness_evidence", [" startup"], id="space-in-evidence"),
        pytest.param("readiness_evidence", ["start-up"], id="punctuation-in-evidence"),
        pytest.param("readiness_evidence", ["st\u00e4rtup"], id="non-ascii-evidence"),
        pytest.param("readiness_evidence", ["startup\n"], id="newline-in-evidence"),
        pytest.param("readiness_evidence", [1], id="non-string-evidence"),
        pytest.param("readiness_evidence", ["init", "init"], id="duplicate-evidence"),
        pytest.param("readiness_timeout", 0, id="zero-timeout"),
        pytest.param("readiness_timeout", True, id="boolean-timeout"),
        pytest.param("readiness_timeout", 10**400, id="unrepresentable-timeout"),
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
