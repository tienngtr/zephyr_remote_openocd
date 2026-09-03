# SPDX-License-Identifier: Apache-2.0

"""Versioned JSON-lines protocol shared by the client and remote helper."""

from __future__ import annotations

import json
from typing import Any, BinaryIO

PROTOCOL_VERSION = 1


class ProtocolError(RuntimeError):
    pass


def encode_message(message_type: str, **fields: Any) -> bytes:
    if not message_type or not isinstance(message_type, str):
        raise ProtocolError("message type must be a non-empty string")
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
    if value.get("version") != PROTOCOL_VERSION:
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
    stream.write(encode_message(message_type, **fields))
    stream.flush()


class EventOrder:
    """Small validator for controller event ordering."""

    def __init__(self) -> None:
        self._state = "new"

    def accept(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        allowed = {
            "new": {"HELLO"},
            "hello": {"SESSION_CREATED", "ERROR"},
            "created": {
                "STAGED",
                "SERVICE_READY",
                "PROCESS_STARTED",
                "CHILD_OUTPUT",
                "PROCESS_EXIT",
                "ERROR",
                "STOPPED",
            },
            "started": {"SERVICE_READY", "CHILD_OUTPUT", "PROCESS_EXIT", "ERROR", "STOPPED"},
            "ready": {"SERVICE_READY", "CHILD_OUTPUT", "PROCESS_EXIT", "ERROR", "STOPPED"},
            "stopped": set(),
        }
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
        elif kind in {"STOPPED", "ERROR"}:
            self._state = "stopped"
