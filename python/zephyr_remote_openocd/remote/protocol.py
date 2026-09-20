# SPDX-License-Identifier: Apache-2.0

"""JSON-lines protocol shared by the client and remote helper."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, BinaryIO

from .model import RemoteProcess, Service

PROTOCOL_VERSION = 1
_ENVELOPE_FIELDS = frozenset(("version", "type"))


class ProtocolError(RuntimeError):
    pass


def is_protocol_version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == PROTOCOL_VERSION


def encode_message(message_type: str, **fields: Any) -> bytes:
    if not message_type or not isinstance(message_type, str):
        raise ProtocolError("message type must be a non-empty string")
    if _ENVELOPE_FIELDS & fields.keys():
        raise ProtocolError("protocol fields must not override version or type")
    message = {"version": PROTOCOL_VERSION, "type": message_type, **fields}
    return (json.dumps(message, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def decode_message(line: bytes | str) -> dict[str, Any]:
    try:
        text = line.decode("utf-8") if isinstance(line, bytes) else line
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"malformed protocol message: {error}") from error
    if not isinstance(value, dict):
        raise ProtocolError("protocol message must be an object")
    if not is_protocol_version(value.get("version")):
        raise ProtocolError(
            f"incompatible protocol version {value.get('version')!r}; expected {PROTOCOL_VERSION}"
        )
    if not isinstance(value.get("type"), str) or not value["type"]:
        raise ProtocolError("protocol message has no valid type")
    return value


def read_message(stream: BinaryIO) -> dict[str, Any]:
    line = stream.readline()
    if not line:
        raise EOFError("helper control channel closed")
    return decode_message(line)


def _write_frame(stream: BinaryIO, message_type: str, **fields: Any) -> None:
    stream.write(encode_message(message_type, **fields))
    stream.flush()


def write_start(stream: BinaryIO, process: RemoteProcess, services: Iterable[Service]) -> None:
    """Serialize a validated process and service model as the START command."""

    _write_frame(
        stream,
        "START",
        argv=list(process.argv),
        environment=dict(process.environment),
        required_paths=[
            {"kind": check.kind, "path": check.path} for check in process.required_paths
        ],
        services=[
            {"name": service.name, "remote_port": service.remote_port} for service in services
        ],
        readiness_marker=process.readiness_marker,
        readiness_timeout=process.readiness_timeout,
        literal_prefix=process.literal_prefix,
    )


def write_stop(stream: BinaryIO) -> None:
    """Serialize the parameterless persistent STOP command."""

    _write_frame(stream, "STOP")


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _has_exact_fields(message: dict[str, Any], fields: frozenset[str]) -> bool:
    return set(message) == _ENVELOPE_FIELDS | fields


_STAGED_FIELDS = frozenset(("byte_count", "sha256", "files"))
_OPENOCD_VERSION_FIELDS = frozenset(("output",))
_DEPLOYMENT_FIELDS = frozenset(("status", "path", "sha256"))


def _valid_session_created(message: dict[str, Any]) -> bool:
    return (
        _non_empty_string(message.get("helper"))
        and _non_empty_string(message.get("session_id"))
        and _non_empty_string(message.get("remote_workspace"))
    )


def _valid_process_ready(message: dict[str, Any]) -> bool:
    child_pid = message.get("child_pid")
    return (
        _non_empty_string(message.get("remote_address"))
        and isinstance(child_pid, int)
        and not isinstance(child_pid, bool)
        and child_pid > 0
    )


def _valid_child_output(message: dict[str, Any]) -> bool:
    return message.get("stream") in {"stdout", "stderr"} and isinstance(message.get("payload"), str)


def _valid_session_closed(message: dict[str, Any]) -> bool:
    reason = message.get("reason")
    return reason in {"requested", "process_exit"} and (
        (reason == "requested" and message.get("returncode") is None)
        or (
            reason == "process_exit"
            and isinstance(message.get("returncode"), int)
            and not isinstance(message.get("returncode"), bool)
        )
    )


def _valid_error(message: dict[str, Any]) -> bool:
    return _non_empty_string(message.get("code")) and isinstance(message.get("message"), str)


_EVENT_FIELDS = {
    "SESSION_CREATED": frozenset(("helper", "session_id", "remote_workspace")),
    "PROCESS_READY": frozenset(("remote_address", "child_pid")),
    "CHILD_OUTPUT": frozenset(("stream", "payload")),
    "SESSION_CLOSED": frozenset(("reason", "returncode")),
    "ERROR": frozenset(("code", "message")),
}
_EVENT_VALIDATORS = {
    "SESSION_CREATED": _valid_session_created,
    "PROCESS_READY": _valid_process_ready,
    "CHILD_OUTPUT": _valid_child_output,
    "SESSION_CLOSED": _valid_session_closed,
    "ERROR": _valid_error,
}


def validate_helper_event(message: dict[str, Any]) -> None:
    """Validate one event in the current persistent helper contract."""

    if not isinstance(message, dict):
        raise ProtocolError("invalid helper event fields")
    kind = message.get("type")
    if not isinstance(kind, str):
        raise ProtocolError("invalid helper event fields")
    fields = _EVENT_FIELDS.get(kind)
    validator = _EVENT_VALIDATORS.get(kind)
    if fields is None or validator is None or not _has_exact_fields(message, fields):
        raise ProtocolError("invalid helper event fields")
    if not validator(message):
        raise ProtocolError(f"invalid required fields for {kind}")


def validate_staged_response(message: dict[str, Any]) -> None:
    if (
        not isinstance(message, dict)
        or not _has_exact_fields(message, _STAGED_FIELDS)
        or message.get("type") != "STAGED"
        or not isinstance(message.get("byte_count"), int)
        or isinstance(message.get("byte_count"), bool)
        or message["byte_count"] < 0
        or not _sha256(message.get("sha256"))
        or not isinstance(message.get("files"), list)
        or not all(isinstance(item, str) for item in message["files"])
    ):
        raise ProtocolError("invalid STAGED response")


def validate_openocd_version_response(message: dict[str, Any]) -> None:
    if (
        not isinstance(message, dict)
        or not _has_exact_fields(message, _OPENOCD_VERSION_FIELDS)
        or message.get("type") != "OPENOCD_VERSION"
        or not isinstance(message.get("output"), str)
    ):
        raise ProtocolError("invalid OPENOCD_VERSION response")


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_deployment_response(message: dict[str, Any]) -> None:
    if (
        not isinstance(message, dict)
        or not _has_exact_fields(message, _DEPLOYMENT_FIELDS)
        or message.get("type") != "DEPLOYED"
        or message.get("status") not in {"deployed", "reused"}
        or not _non_empty_string(message.get("path"))
        or not _sha256(message.get("sha256"))
    ):
        raise ProtocolError("invalid deployment response")


class EventOrder:
    """Validate the persistent helper event lifecycle."""

    def __init__(self) -> None:
        self._state = "new"

    def accept(self, message: dict[str, Any]) -> None:
        validate_helper_event(message)
        kind = message["type"]
        if kind not in _EVENT_TRANSITIONS[self._state]:
            raise ProtocolError(f"unexpected {kind} event in {self._state} state")
        if kind != "CHILD_OUTPUT":
            self._state = _EVENT_NEXT_STATE[kind]


_EVENT_TRANSITIONS = {
    "new": frozenset(("SESSION_CREATED", "ERROR")),
    "created": frozenset(("PROCESS_READY", "CHILD_OUTPUT", "SESSION_CLOSED", "ERROR")),
    "active": frozenset(("CHILD_OUTPUT", "SESSION_CLOSED", "ERROR")),
    "closed": frozenset(),
}

_EVENT_NEXT_STATE = {
    "SESSION_CREATED": "created",
    "PROCESS_READY": "active",
    "CHILD_OUTPUT": "active",
    "SESSION_CLOSED": "closed",
    "ERROR": "closed",
}
