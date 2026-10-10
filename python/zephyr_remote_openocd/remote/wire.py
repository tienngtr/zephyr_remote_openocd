# SPDX-License-Identifier: Apache-2.0

"""Bounded Protocol v2 framing and structured terminal transport.

This module depends only on the result domain and the standard library so the
same codec can be bundled with the remote helper.
"""

from __future__ import annotations

import ipaddress
import json
import math
from dataclasses import asdict
from typing import Any

from .outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    Outcome,
    TerminalSnapshot,
    Trigger,
)

VERSION = 2
MAX_FRAME_SIZE = 1024 * 1024
_RANGE = ipaddress.IPv4Network("127.64.0.0/10")
_RESIDUALS = frozenset(
    (
        "child_producer",
        "child_group",
        "child_relays",
        "address_lease",
        "workspace",
        "workspace_metadata",
    )
)


class ProtocolError(RuntimeError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate protocol object key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ProtocolError(f"non-finite protocol number: {value}")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _nonfinite(value)
    return number


def decode_frame(frame: bytes | str) -> dict[str, Any]:
    """Decode one complete bounded frame without permissive JSON extensions."""
    try:
        data = frame.encode("utf-8") if isinstance(frame, str) else frame
        if len(data) > MAX_FRAME_SIZE:
            raise ProtocolError("protocol frame exceeds maximum size")
        if not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise ProtocolError("protocol frame must have exactly one LF delimiter")
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_nonfinite,
            parse_float=_float,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ProtocolError(f"malformed protocol frame: {error}") from error
    if (
        not isinstance(value, dict)
        or not integer(value.get("version"))
        or value["version"] != VERSION
        or not string(value.get("type"), nonempty=True)
    ):
        raise ProtocolError("incompatible protocol envelope")
    return value


def _check_render_values(value: Any) -> None:
    """Bound each JSON encoder fragment before rendering a large string."""
    if isinstance(value, str):
        if len(value) > MAX_FRAME_SIZE:
            raise ProtocolError("protocol frame exceeds maximum size")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_render_values(key)
            _check_render_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_render_values(item)


def encode_frame(kind: str, **fields: Any) -> bytes:
    if not string(kind, nonempty=True) or {"version", "type"} & fields.keys():
        raise ProtocolError("invalid protocol envelope")
    try:
        value = {"version": VERSION, "type": kind, **fields}
        _check_render_values(value)
        encoder = json.JSONEncoder(separators=(",", ":"), sort_keys=True, allow_nan=False)
        data = bytearray()
        for fragment in encoder.iterencode(value):
            encoded = fragment.encode("utf-8")
            if len(data) + len(encoded) + 1 > MAX_FRAME_SIZE:
                raise ProtocolError("protocol frame exceeds maximum size")
            data.extend(encoded)
        data.extend(b"\n")
    except (TypeError, ValueError, RecursionError) as error:
        raise ProtocolError(f"cannot encode protocol frame: {error}") from error
    return bytes(data)


def integer(value: Any, *, low: int | None = None, high: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (low is None or value >= low)
        and (high is None or value <= high)
    )


def string(value: Any, *, nonempty: bool = False, no_nul: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (not nonempty or bool(value))
        and (not no_nul or "\0" not in value)
    )


def _exact(value: Any, fields: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == fields


def _generation(value: Any) -> bool:
    return integer(value, low=1, high=32)


def _diagnostic(value: Any) -> Diagnostic:
    if (
        not _exact(value, {"code", "message", "diagnostics"})
        or not string(value["code"], nonempty=True)
        or not string(value["message"])
        or not isinstance(value["diagnostics"], list)
    ):
        raise ProtocolError("invalid diagnostic")
    return Diagnostic(
        value["code"], value["message"], tuple(_diagnostic(item) for item in value["diagnostics"])
    )


def _child_result(value: Any) -> ChildResult | None:
    if value is None:
        return None
    if (
        not _exact(value, {"generation", "returncode", "termination_requested"})
        or not _generation(value["generation"])
        or not integer(value["returncode"])
        or not isinstance(value["termination_requested"], bool)
    ):
        raise ProtocolError("invalid child result")
    return ChildResult(**value)


def _cleanup(value: Any) -> CleanupReport:
    if (
        not _exact(value, {"child_disposal", "workspace_disposal", "residual_resources"})
        or not isinstance(value["child_disposal"], str)
        or value["child_disposal"] not in {"not_acquired", "confirmed", "unconfirmed"}
        or not isinstance(value["workspace_disposal"], str)
        or value["workspace_disposal"] not in {"not_created", "confirmed", "unconfirmed"}
        or not isinstance(value["residual_resources"], list)
        or not all(
            isinstance(item, str) and item in _RESIDUALS for item in value["residual_resources"]
        )
    ):
        raise ProtocolError("invalid cleanup report")
    return CleanupReport(**value)


def decode_terminal(message: dict[str, Any]) -> TerminalSnapshot:
    try:
        if (
            not _exact(
                message,
                {
                    "version",
                    "type",
                    "trigger",
                    "primary_failure",
                    "diagnostics",
                    "child_result",
                    "cleanup",
                },
            )
            or message["type"] != "SESSION_ENDED"
            or not integer(message["version"])
            or message["version"] != VERSION
        ):
            raise ProtocolError("invalid terminal fields")
        if not isinstance(message["diagnostics"], list):
            raise ProtocolError("invalid terminal diagnostics")
        outcome = Outcome(
            Trigger(message["trigger"]),
            None if message["primary_failure"] is None else _diagnostic(message["primary_failure"]),
            tuple(_diagnostic(item) for item in message["diagnostics"]),
            _child_result(message["child_result"]),
        )
        return TerminalSnapshot(outcome, _cleanup(message["cleanup"]))
    except (ValueError, TypeError, RecursionError) as error:
        raise ProtocolError(f"invalid terminal outcome: {error}") from error


def terminal_fields(snapshot: TerminalSnapshot) -> dict[str, Any]:
    fields = {**asdict(snapshot.outcome), "cleanup": asdict(snapshot.cleanup)}
    # Convert tuple collections to JSON arrays and validate all domain fields
    # before writer admission; no truncated substitute terminal is fabricated.
    decode_terminal(decode_frame(encode_frame("SESSION_ENDED", **fields)))
    return fields


def _valid_address(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return (
        str(address) == value
        and address in _RANGE
        and address not in (_RANGE.network_address, _RANGE.broadcast_address)
    )


def validate_event(message: dict[str, Any]) -> None:
    kind = message.get("type")
    fields = _FIELDS.get(kind) if isinstance(kind, str) else None
    if (
        fields is None
        or not _exact(message, {"version", "type"} | fields)
        or not integer(message.get("version"))
        or message["version"] != VERSION
    ):
        raise ProtocolError("invalid helper event fields")
    valid = True
    if kind == "SESSION_CREATED":
        valid = all(string(message[field], nonempty=True, no_nul=True) for field in fields)
    elif kind == "ATTEMPT":
        argv = message["argv"]
        valid = (
            _generation(message["generation"])
            and isinstance(argv, list)
            and bool(argv)
            and string(argv[0], nonempty=True, no_nul=True)
            and all(string(arg, no_nul=True) for arg in argv[1:])
        )
    elif kind == "READY":
        valid = (
            _generation(message["generation"])
            and _valid_address(message["remote_address"])
            and integer(message["child_pid"], low=1)
        )
    elif kind == "CHILD_OUTPUT":
        valid = (
            _generation(message["generation"])
            and isinstance(message["stream"], str)
            and message["stream"] in {"stdout", "stderr"}
            and string(message["payload"])
            and "\n" not in message["payload"]
            and isinstance(message["line_end"], bool)
            and (bool(message["payload"]) or message["line_end"])
        )
    elif kind == "SESSION_ENDED":
        decode_terminal(message)
    if not valid:
        raise ProtocolError(f"invalid required fields for {kind}")


_FIELDS = {
    "SESSION_CREATED": {"helper", "session_id", "remote_workspace"},
    "ATTEMPT": {"generation", "argv"},
    "READY": {"generation", "remote_address", "child_pid"},
    "CHILD_OUTPUT": {"generation", "stream", "payload", "line_end"},
    "SESSION_ENDED": {"trigger", "primary_failure", "diagnostics", "child_result", "cleanup"},
}


class EventOrder:
    """Validate admitted attempt identity, operation policy, and terminal uniqueness."""

    def __init__(self, policy: CompletionPolicy = CompletionPolicy.LIVE_SERVER) -> None:
        self.policy = policy
        self.created = False
        self.generation = 0
        self.ready = False
        self.ended = False

    def accept(self, message: dict[str, Any]) -> None:
        validate_event(message)
        kind = message["type"]
        if self.ended or (not self.created and kind not in {"SESSION_CREATED", "SESSION_ENDED"}):
            raise ProtocolError("event outside session lifetime")
        if kind == "SESSION_CREATED":
            if self.created:
                raise ProtocolError("duplicate session creation")
            self.created = True
        elif kind == "ATTEMPT":
            if self.ready or message["generation"] != self.generation + 1:
                raise ProtocolError("invalid attempt sequence")
            self.generation = message["generation"]
        elif kind == "READY":
            if (
                self.policy != CompletionPolicy.LIVE_SERVER
                or self.ready
                or not self.generation
                or message["generation"] != self.generation
            ):
                raise ProtocolError("invalid readiness admission")
            self.ready = True
        elif kind == "CHILD_OUTPUT":
            if message["generation"] > self.generation:
                raise ProtocolError("output from an unadmitted attempt")
        elif kind == "SESSION_ENDED":
            result = decode_terminal(message).outcome.child_result
            if result is not None and result.generation != self.generation:
                raise ProtocolError("terminal result is not from the final attempt")
            self.ended = True
