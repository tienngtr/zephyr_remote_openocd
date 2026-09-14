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
    validate_client_command(decode_message(encoded))
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


def _unique_service_ports(services: list[dict[str, Any]]) -> bool:
    ports = [service["remote_port"] for service in services]
    return len(ports) == len(set(ports))


def _address(value: Any) -> bool:
    return _non_empty_string(value)


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_start_command(message: dict[str, Any]) -> None:
    services = message.get("services")
    valid = (
        isinstance(services, list)
        and services
        and all(_fake_service(item) for item in services)
        and _unique_service_ports(services)
    )
    if not valid:
        raise ProtocolError("invalid START command")


def _valid_environment(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _valid_path_checks(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and item.get("kind") in {"file", "directory"}
        and isinstance(item.get("path"), str)
        for item in value
    )


def _valid_marker(value: Any) -> bool:
    return value is None or (_non_empty_string(value) and not any(char.isspace() for char in value))


def _valid_timeout(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _valid_literal_prefix(value: Any, argv_length: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= argv_length


def _validate_start_openocd_command(message: dict[str, Any]) -> None:
    argv = message.get("argv")
    environment = message.get("environment", {})
    checks = message.get("required_paths", [])
    services = message.get("services", [])
    marker = message.get("readiness_marker")
    timeout = message.get("readiness_timeout", 30.0)
    literal_prefix = message.get("literal_prefix", 0)
    if (
        not isinstance(argv, list)
        or not argv
        or not _non_empty_string(argv[0])
        or not all(isinstance(item, str) for item in argv[1:])
        or not _valid_environment(environment)
        or not _valid_path_checks(checks)
        or not isinstance(services, list)
        or not all(_service(item) for item in services)
        or not _unique_service_ports(services)
        or not _valid_marker(marker)
        or not _valid_timeout(timeout)
        or not _valid_literal_prefix(literal_prefix, len(argv) if isinstance(argv, list) else 0)
    ):
        raise ProtocolError("invalid START_OPENOCD command")


def validate_client_command(message: dict[str, Any]) -> None:
    """Validate the required fields of a Protocol v1 client command."""

    kind = message["type"]
    if kind == "STOP":
        return
    validators = {
        "START": _validate_start_command,
        "START_OPENOCD": _validate_start_openocd_command,
    }
    validator = validators.get(kind)
    if validator is None:
        raise ProtocolError(f"unexpected client command type: {kind!r}")
    validator(message)


def _valid_hello(message: dict[str, Any]) -> bool:
    return _non_empty_string(message.get("helper"))


def _valid_session_created(message: dict[str, Any]) -> bool:
    return _non_empty_string(message.get("session_id")) and _non_empty_string(
        message.get("remote_workspace")
    )


def _valid_process_started(message: dict[str, Any]) -> bool:
    child_pid = message.get("child_pid")
    return (
        _address(message.get("remote_address"))
        and isinstance(child_pid, int)
        and not isinstance(child_pid, bool)
        and child_pid > 0
    )


def _valid_service_ready(message: dict[str, Any]) -> bool:
    child_pid = message.get("child_pid")
    fake_services = message.get("services")
    return _address(message.get("remote_address")) and (
        _service(message.get("service"))
        or (
            isinstance(fake_services, list)
            and bool(fake_services)
            and all(_fake_service(item) for item in fake_services)
            and isinstance(child_pid, int)
            and not isinstance(child_pid, bool)
            and child_pid > 0
        )
    )


def _valid_child_output(message: dict[str, Any]) -> bool:
    return message.get("stream") in {"stdout", "stderr"} and isinstance(message.get("payload"), str)


def _valid_process_exit(message: dict[str, Any]) -> bool:
    return isinstance(message.get("returncode"), int) and not isinstance(
        message.get("returncode"), bool
    )


def _valid_stopped(message: dict[str, Any]) -> bool:
    return message.get("reason") in {"requested", "process_exit"}


def _valid_error(message: dict[str, Any]) -> bool:
    return _non_empty_string(message.get("code")) and isinstance(message.get("message"), str)


_EVENT_VALIDATORS = {
    "HELLO": _valid_hello,
    "SESSION_CREATED": _valid_session_created,
    "PROCESS_STARTED": _valid_process_started,
    "SERVICE_READY": _valid_service_ready,
    "CHILD_OUTPUT": _valid_child_output,
    "PROCESS_EXIT": _valid_process_exit,
    "STOPPED": _valid_stopped,
    "ERROR": _valid_error,
}


def validate_helper_event(message: dict[str, Any]) -> None:
    """Validate the required fields of a Protocol v1 helper event.

    Unknown fields are deliberately ignored: they were accepted by the original
    helper/client implementation and are not a Protocol v1 extension point.
    """

    kind = message["type"]
    validator = _EVENT_VALIDATORS.get(kind)
    if validator is None:
        raise ProtocolError(f"unexpected helper event type: {kind!r}")
    if not validator(message):
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
    """Validate helper event ordering."""

    def __init__(self) -> None:
        self._state = "new"

    def accept(self, message: dict[str, Any]) -> None:
        validate_helper_event(message)
        kind = message["type"]
        allowed = _EVENT_TRANSITIONS[self._state]
        # The aggregate SERVICE_READY form belongs only to the test fake
        # service. A real service-ready event is valid only after the child
        # process has been announced with PROCESS_STARTED.
        aggregate_ready = (
            self._state == "created"
            and kind == "SERVICE_READY"
            and isinstance(message.get("services"), list)
        )
        if kind not in allowed and not aggregate_ready:
            raise ProtocolError(f"unexpected {kind} event in {self._state} state")
        if kind != "CHILD_OUTPUT":
            self._state = _EVENT_NEXT_STATE[kind]


_EVENT_TRANSITIONS = {
    "new": frozenset({"HELLO"}),
    "hello": frozenset({"SESSION_CREATED", "ERROR"}),
    "created": frozenset({"PROCESS_STARTED", "CHILD_OUTPUT", "ERROR", "STOPPED"}),
    "started": frozenset({"SERVICE_READY", "CHILD_OUTPUT", "PROCESS_EXIT", "ERROR", "STOPPED"}),
    "ready": frozenset({"SERVICE_READY", "CHILD_OUTPUT", "PROCESS_EXIT", "ERROR", "STOPPED"}),
    "exited": frozenset({"STOPPED"}),
    "stopped": frozenset(),
}

_EVENT_NEXT_STATE = {
    "HELLO": "hello",
    "SESSION_CREATED": "created",
    "PROCESS_STARTED": "started",
    "SERVICE_READY": "ready",
    "PROCESS_EXIT": "exited",
    "STOPPED": "stopped",
    "ERROR": "stopped",
}
