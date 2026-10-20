# SPDX-License-Identifier: Apache-2.0

"""Bounded Protocol v1 framing and structured terminal transport.

This module depends only on the result domain and the standard library so the
same codec can be bundled with the remote helper.
"""

from __future__ import annotations

import ipaddress
import json
import math
from dataclasses import asdict
from typing import Any, TypeGuard

from zephyr_remote_openocd.protocol.addresses import LOOPBACK_RANGE
from zephyr_remote_openocd.protocol.outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    Outcome,
    TerminalSnapshot,
    Trigger,
)

VERSION = 1
MAX_FRAME_SIZE = 1024 * 1024
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


def _require_scalar_text(text: str) -> None:
    """Reject isolated surrogates, which JSON escapes express but UTF-8 cannot."""
    if not text.isascii():
        text.encode("utf-8")


def _require_scalar_strings(value: object) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            _require_scalar_text(item)
        elif isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


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
        _require_scalar_strings(value)
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


def _check_render_values(value: object) -> None:
    """Bound each JSON encoder fragment and require scalar text before rendering."""
    if isinstance(value, str):
        if len(value) > MAX_FRAME_SIZE:
            raise ProtocolError("protocol frame exceeds maximum size")
        _require_scalar_text(value)
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


def integer(value: object, *, low: int | None = None, high: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (low is None or value >= low)
        and (high is None or value <= high)
    )


def string(value: object, *, nonempty: bool = False, no_nul: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (not nonempty or bool(value))
        and (not no_nul or "\0" not in value)
    )


def _exact(value: object, fields: set[str]) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict) and set(value) == fields


def _diagnostic(value: object) -> Diagnostic:
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


def _child_result(value: object) -> ChildResult | None:
    if value is None:
        return None
    if (
        not _exact(value, {"returncode", "termination_requested"})
        or not integer(value["returncode"])
        or not isinstance(value["termination_requested"], bool)
    ):
        raise ProtocolError("invalid child result")
    return ChildResult(**value)


def _cleanup(value: object) -> CleanupReport:
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


def _valid_address(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return (
        str(address) == value
        and address in LOOPBACK_RANGE
        and address not in (LOOPBACK_RANGE.network_address, LOOPBACK_RANGE.broadcast_address)
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
            isinstance(argv, list)
            and bool(argv)
            and string(argv[0], nonempty=True, no_nul=True)
            and all(string(arg, no_nul=True) for arg in argv[1:])
        )
    elif kind == "READY":
        valid = _valid_address(message["remote_address"]) and integer(message["child_pid"], low=1)
    elif kind == "CHILD_OUTPUT":
        valid = (
            isinstance(message["stream"], str)
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
    "ATTEMPT": {"argv"},
    "READY": {"remote_address", "child_pid"},
    "CHILD_OUTPUT": {"stream", "payload", "line_end"},
    "SESSION_ENDED": {"trigger", "primary_failure", "diagnostics", "child_result", "cleanup"},
}


class EventOrder:
    """Validate admission and terminal claims against the observed history."""

    def __init__(self, policy: CompletionPolicy = CompletionPolicy.LIVE_SERVER) -> None:
        self.policy = policy
        self.created = False
        self.attempted = False
        self.ready = False
        self.child_output_seen = False
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
            if self.attempted:
                raise ProtocolError("invalid attempt sequence")
            self.attempted = True
        elif kind == "READY":
            if self.policy != CompletionPolicy.LIVE_SERVER or self.ready or not self.attempted:
                raise ProtocolError("invalid readiness admission")
            self.ready = True
        elif kind == "CHILD_OUTPUT":
            if not self.attempted:
                raise ProtocolError("output from an unadmitted attempt")
            self.child_output_seen = True
        elif kind == "SESSION_ENDED":
            snapshot = decode_terminal(message)
            result = snapshot.outcome.child_result
            if result is not None and not self.attempted:
                raise ProtocolError("child result without an admitted attempt")
            if self.created and snapshot.cleanup.workspace_disposal == "not_created":
                raise ProtocolError("created workspace reported as not created")
            if (
                self.ready or self.child_output_seen
            ) and snapshot.cleanup.child_disposal == "not_acquired":
                raise ProtocolError("observed child reported as not acquired")
            self.ended = True


class ControlFrameBuffer:
    """Incrementally frame bounded LF-delimited control input."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def has_pending_bytes(self) -> bool:
        """Report trailing input without exposing the mutable frame storage."""
        return bool(self._buffer)

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    def pop_frame(self) -> bytes | None:
        delimiter = self._buffer.find(b"\n")
        size = delimiter + 1 if delimiter >= 0 else len(self._buffer) + 1
        if size > MAX_FRAME_SIZE:
            raise ValueError("protocol frame exceeds maximum size")
        if delimiter < 0:
            return None
        frame = bytes(self._buffer[: delimiter + 1])
        del self._buffer[: delimiter + 1]
        return frame

    def finish(self) -> None:
        if self._buffer:
            raise ValueError("protocol frame is missing its LF delimiter")
