# SPDX-License-Identifier: Apache-2.0

"""Versioned JSON-lines protocol shared by the client and remote helper."""

from __future__ import annotations

import json
from typing import Any, BinaryIO

PROTOCOL_VERSION = 1


class ProtocolError(RuntimeError):
    pass


def is_protocol_version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == PROTOCOL_VERSION


def encode_message(message_type: str, **fields: Any) -> bytes:
    if not message_type or not isinstance(message_type, str):
        raise ProtocolError("message type must be a non-empty string")
    if {"version", "type"} & fields.keys():
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


def write_message(stream: BinaryIO, message_type: str, **fields: Any) -> None:
    encoded = encode_message(message_type, **fields)
    validate_controller_command(decode_message(encoded))
    stream.write(encoded)
    stream.flush()


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _port(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def _service(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("name"), str)
        and _port(value.get("remote_port"))
    )


def _fake_service(value: Any) -> bool:
    return isinstance(value, dict) and _port(value.get("remote_port"))


def _address(value: Any) -> bool:
    return _non_empty_string(value)


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_controller_command(message: dict[str, Any]) -> None:
    """Validate the frozen protocol-1 controller command fields."""

    kind = message["type"]
    if kind == "STOP":
        return
    if kind == "START":
        services = message.get("services")
        if (
            isinstance(services, list)
            and services
            and all(_fake_service(item) for item in services)
        ):
            return
        raise ProtocolError("invalid START command")
    if kind != "START_OPENOCD":
        raise ProtocolError(f"unexpected controller command type: {kind!r}")
    argv = message.get("argv")
    environment = message.get("environment", {})
    checks = message.get("required_paths", [])
    services = message.get("services", [])
    marker = message.get("readiness_marker")
    timeout = message.get("readiness_timeout", 30.0)
    if (
        not isinstance(argv, list)
        or not argv
        or not all(_non_empty_string(item) for item in argv)
        or not isinstance(environment, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
        )
        or not isinstance(checks, list)
        or not all(
            isinstance(item, dict)
            and item.get("kind") in {"file", "directory"}
            and isinstance(item.get("path"), str)
            for item in checks
        )
        or not isinstance(services, list)
        or not all(_service(item) for item in services)
        or (
            marker is not None
            and (not _non_empty_string(marker) or any(char.isspace() for char in marker))
        )
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
    ):
        raise ProtocolError("invalid START_OPENOCD command")


def validate_controller_event(message: dict[str, Any]) -> None:
    """Validate the fixed required fields of a protocol-1 controller event.

    Unknown fields are deliberately ignored: they were accepted by the original
    helper/client implementation and are not a protocol-1 extension point.
    """

    kind = message["type"]
    valid = {
        "HELLO": lambda: _non_empty_string(message.get("helper")),
        "SESSION_CREATED": lambda: (
            _non_empty_string(message.get("session_id"))
            and _non_empty_string(message.get("remote_workspace"))
        ),
        "PROCESS_STARTED": lambda: (
            _address(message.get("remote_address"))
            and isinstance(message.get("child_pid"), int)
            and not isinstance(message.get("child_pid"), bool)
            and message["child_pid"] > 0
        ),
        "SERVICE_READY": lambda: (
            _address(message.get("remote_address"))
            and (
                _service(message.get("service"))
                or (
                    isinstance(message.get("services"), list)
                    and bool(message["services"])
                    and all(_fake_service(item) for item in message["services"])
                    and isinstance(message.get("child_pid"), int)
                    and not isinstance(message.get("child_pid"), bool)
                    and message["child_pid"] > 0
                )
            )
        ),
        "CHILD_OUTPUT": lambda: (
            message.get("stream") in {"stdout", "stderr"}
            and isinstance(message.get("payload"), str)
        ),
        "PROCESS_EXIT": lambda: (
            isinstance(message.get("returncode"), int)
            and not isinstance(message.get("returncode"), bool)
        ),
        "STOPPED": lambda: _non_empty_string(message.get("reason")),
        "ERROR": lambda: (
            _non_empty_string(message.get("code")) and isinstance(message.get("message"), str)
        ),
    }
    if kind not in valid:
        raise ProtocolError(f"unexpected controller event type: {kind!r}")
    if not valid[kind]():
        raise ProtocolError(f"invalid required fields for {kind}")


def validate_staged_response(message: dict[str, Any]) -> None:
    if (
        message["type"] != "STAGED"
        or not isinstance(message.get("byte_count"), int)
        or isinstance(message.get("byte_count"), bool)
        or message["byte_count"] < 0
        or not _sha256(message.get("sha256"))
        or not isinstance(message.get("files"), list)
        or not all(isinstance(item, str) for item in message["files"])
    ):
        raise ProtocolError("invalid STAGED response")


def validate_openocd_version_response(message: dict[str, Any]) -> None:
    if message["type"] != "OPENOCD_VERSION" or not isinstance(message.get("output"), str):
        raise ProtocolError("invalid OPENOCD_VERSION response")


def validate_deployment_response(message: dict[str, Any]) -> None:
    if (
        message["type"] != "DEPLOYED"
        or message.get("status") not in {"deployed", "reused"}
        or not _non_empty_string(message.get("path"))
        or not _sha256(message.get("sha256"))
    ):
        raise ProtocolError("invalid DEPLOYED response")


class EventOrder:
    """Small validator for controller event ordering."""

    def __init__(self) -> None:
        self._state = "new"

    def accept(self, message: dict[str, Any]) -> None:
        validate_controller_event(message)
        kind = message["type"]
        allowed = {
            "new": {"HELLO"},
            "hello": {"SESSION_CREATED", "ERROR"},
            "created": {
                "PROCESS_STARTED",
                "CHILD_OUTPUT",
                "ERROR",
                "STOPPED",
            },
            "started": {"SERVICE_READY", "CHILD_OUTPUT", "PROCESS_EXIT", "ERROR", "STOPPED"},
            "ready": {"SERVICE_READY", "CHILD_OUTPUT", "PROCESS_EXIT", "ERROR", "STOPPED"},
            "exited": {"STOPPED"},
            "stopped": set(),
        }
        # The aggregate SERVICE_READY form belongs only to the test fake
        # service. A real service-ready event is valid only after the child
        # process has been announced with PROCESS_STARTED.
        if (
            self._state == "created"
            and kind == "SERVICE_READY"
            and isinstance(message.get("services"), list)
        ):
            allowed["created"].add(kind)
        if kind not in allowed[self._state]:
            raise ProtocolError(f"unexpected {kind} event in {self._state} state")
        if kind == "HELLO":
            self._state = "hello"
        elif kind == "SESSION_CREATED":
            self._state = "created"
        elif kind == "PROCESS_STARTED":
            self._state = "started"
        elif kind == "SERVICE_READY":
            self._state = "ready"
        elif kind == "PROCESS_EXIT":
            self._state = "exited"
        elif kind in {"STOPPED", "ERROR"}:
            self._state = "stopped"
