# SPDX-License-Identifier: Apache-2.0

"""Strict Protocol v1 START decoding and serialization.

Validation ownership is layered. `decode_frame` owns framing and the envelope.
START decoding owns wire shape and context: exact field sets, JSON types for
values without a domain model, cross-field indexes, and where Tcl words may
appear. Domain constructors (`ListenerSpec`, `ArgumentTemplate`, `TclWord`,
`SessionValue`, `CompletionPolicy`) own value invariants, and decoding always
constructs them rather than restating their checks.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any, BinaryIO, NamedTuple

from zephyr_remote_openocd.protocol.addresses import validate_preferred_address
from zephyr_remote_openocd.protocol.arguments import ArgumentTemplate, SessionValue, TclWord
from zephyr_remote_openocd.protocol.listeners import ListenerSpec, validated_listeners
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import decode_frame, encode_frame


def _parse_listeners(listeners: object) -> tuple[ListenerSpec, ...]:
    if not isinstance(listeners, list) or not all(
        isinstance(item, dict) and set(item) == {"name", "remote_port"} for item in listeners
    ):
        raise ValueError("START services are invalid")
    return validated_listeners(ListenerSpec(**item) for item in listeners)


def validate_argv(argv: object) -> None:
    """Validate a START argv list before admission or materialized execution."""
    if not isinstance(argv, list) or not argv:
        raise ValueError("START requires a non-empty string argv")
    if not isinstance(argv[0], str) or not argv[0]:
        raise ValueError("START requires a non-empty string argv")
    if not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
        raise ValueError("START requires a non-empty string argv")


def _validate_environment(environment: object) -> None:
    if not isinstance(environment, dict):
        raise ValueError("START environment must contain valid string values")
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise ValueError("START environment must contain valid string values")


def _validate_required_output_sentinels(sentinels: object) -> None:
    if not isinstance(sentinels, list) or not all(
        isinstance(sentinel, str)
        and sentinel
        and sentinel == sentinel.strip()
        and "\0" not in sentinel
        and "\n" not in sentinel
        and "\r" not in sentinel
        for sentinel in sentinels
    ):
        raise ValueError("START required output markers are invalid")
    if len(sentinels) != len(set(sentinels)):
        raise ValueError("START required output markers must be unique")


def _validate_timeout(timeout: object) -> None:
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("START readiness options are invalid")


def _validate_literal_prefix(literal_prefix: object, argv_length: int) -> None:
    if (
        isinstance(literal_prefix, bool)
        or not isinstance(literal_prefix, int)
        or not 0 <= literal_prefix <= argv_length
    ):
        raise ValueError("START readiness options are invalid")


def _validate_options(
    sentinels: object, timeout: object, literal_prefix: object, argv_length: int
) -> None:
    _validate_required_output_sentinels(sentinels)
    _validate_timeout(timeout)
    _validate_literal_prefix(literal_prefix, argv_length)


def _parse_plain_part(value: object) -> str | SessionValue:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"session"}:
        return SessionValue(value["session"])
    raise ValueError("invalid argument template part")


def _parse_plain_parts(values: object) -> tuple[str | SessionValue, ...]:
    if not isinstance(values, list) or not values:
        raise ValueError("invalid argument template parts")
    return tuple(_parse_plain_part(value) for value in values)


def _parse_argv_parts(values: object) -> tuple[str | SessionValue | TclWord, ...]:
    # Tcl words are argv-only and contain no nested words. Enforcing that here
    # keeps paths free of Tcl quoting and bounds recursion on hostile input.
    if not isinstance(values, list) or not values:
        raise ValueError("invalid argument template parts")
    return tuple(
        TclWord(_parse_plain_parts(value["tcl_word"]))
        if isinstance(value, dict) and set(value) == {"tcl_word"}
        else _parse_plain_part(value)
        for value in values
    )


def _parse_argv_templates(
    values: object, argv_length: int, literal_prefix: int
) -> tuple[tuple[int, ArgumentTemplate], ...]:
    if not isinstance(values, list):
        raise ValueError("invalid argv templates")
    templates = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"index", "parts"}:
            raise ValueError("invalid argv template")
        index = value["index"]
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not literal_prefix <= index < argv_length
            or index in seen
        ):
            raise ValueError("invalid argv template index")
        seen.add(index)
        templates.append((index, ArgumentTemplate(_parse_argv_parts(value["parts"]))))
    return tuple(templates)


class RequiredPath(NamedTuple):
    kind: str
    path: str | ArgumentTemplate


class StartRequest(NamedTuple):
    completion_policy: CompletionPolicy
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    required_paths: tuple[RequiredPath, ...]
    listeners: tuple[ListenerSpec, ...]
    required_output_sentinels: tuple[str, ...]
    readiness_timeout: float
    literal_prefix: int
    argv_templates: tuple[tuple[int, ArgumentTemplate], ...]
    preferred_address: str | None


def _parse_required_paths(values: object) -> tuple[RequiredPath, ...]:
    if not isinstance(values, list):
        raise ValueError("invalid required-path assertion")
    return tuple(_parse_required_path(item) for item in values)


def _parse_required_path(item: object) -> RequiredPath:
    if (
        not isinstance(item, dict)
        or set(item) != {"kind", "path"}
        or item.get("kind") not in ("file", "directory")
    ):
        raise ValueError("invalid required-path assertion")
    path = item["path"]
    if isinstance(path, str):
        if not path or "\0" in path:
            raise ValueError("invalid required-path assertion")
    elif isinstance(path, dict) and set(path) == {"parts"}:
        path = ArgumentTemplate(_parse_plain_parts(path["parts"]))
    else:
        raise ValueError("invalid required-path assertion")
    return RequiredPath(item["kind"], path)


def _decode_start(message: dict[str, Any]) -> StartRequest:
    fields = {
        "version",
        "type",
        "argv",
        "environment",
        "required_paths",
        "services",
        "required_output_sentinels",
        "readiness_timeout",
        "literal_prefix",
        "argv_templates",
        "preferred_address",
        "completion_policy",
    }
    if set(message) != fields:
        raise ValueError("START fields are invalid")
    argv = message["argv"]
    environment = message["environment"]
    sentinels = message["required_output_sentinels"]
    timeout = message["readiness_timeout"]
    literal_prefix = message["literal_prefix"]
    validate_argv(argv)
    _validate_environment(environment)
    _validate_options(sentinels, timeout, literal_prefix, len(argv))
    listeners = _parse_listeners(message["services"])
    checks = _parse_required_paths(message["required_paths"])
    templates = _parse_argv_templates(message["argv_templates"], len(argv), literal_prefix)
    preferred_address = message["preferred_address"]
    validate_preferred_address(preferred_address)
    return StartRequest(
        CompletionPolicy(message["completion_policy"]),
        tuple(argv),
        tuple(environment.items()),
        checks,
        listeners,
        tuple(sentinels),
        float(timeout),
        literal_prefix,
        templates,
        preferred_address,
    )


def decode_command(frame: bytes) -> StartRequest:
    """Decode and validate one control frame into an immutable request."""
    message = decode_frame(frame)
    if message["type"] == "START":
        return _decode_start(message)
    raise ValueError("only START is valid on controller input")


def encode_start(
    process: ProcessSpec,
    listeners: Iterable[ListenerSpec],
    *,
    preferred_address: str | None = None,
) -> bytes:
    """Serialize a validated process and listener model as one START frame."""

    validate_preferred_address(preferred_address)
    return encode_frame(
        "START",
        completion_policy=process.completion_policy.value,
        preferred_address=preferred_address,
        argv=list(process.argv),
        environment=dict(process.environment),
        required_paths=[
            {
                "kind": check.kind,
                "path": check.path
                if check.template is None
                else {"parts": check.template.wire_parts()},
            }
            for check in process.required_paths
        ],
        services=[
            {"name": listener.name, "remote_port": listener.remote_port} for listener in listeners
        ],
        required_output_sentinels=list(process.required_output_sentinels),
        readiness_timeout=process.readiness_timeout,
        literal_prefix=process.literal_prefix,
        argv_templates=[
            {"index": index, "parts": template.wire_parts()}
            for index, template in process.argv_templates
        ],
    )


def write_start(
    stream: BinaryIO,
    process: ProcessSpec,
    listeners: Iterable[ListenerSpec],
    *,
    preferred_address: str | None = None,
) -> None:
    """Write the START frame for a validated process and listener model."""

    stream.write(encode_start(process, listeners, preferred_address=preferred_address))
    stream.flush()
