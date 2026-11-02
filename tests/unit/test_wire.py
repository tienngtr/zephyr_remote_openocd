# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

import pytest
from zephyr_remote_openocd.protocol.outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    LogReport,
    Outcome,
    TerminalSnapshot,
    Trigger,
)
from zephyr_remote_openocd.protocol.wire import (
    MAX_FRAME_SIZE,
    MAX_LOG_DATA_BYTES,
    EventOrder,
    ProtocolError,
    decode_frame,
    decode_terminal,
    encode_frame,
    encode_log_data,
    log_data,
    terminal_fields,
)

ChildDisposal = Literal["not_acquired", "confirmed", "unconfirmed"]
WorkspaceDisposal = Literal["not_created", "confirmed", "unconfirmed"]


def _event(kind: str, **fields: object) -> dict[str, Any]:
    return decode_frame(encode_frame(kind, **fields))


def _terminal() -> TerminalSnapshot:
    return TerminalSnapshot(
        Outcome(Trigger.CHILD_EXIT, child_result=ChildResult(0, False)),
        CleanupReport("confirmed", "confirmed"),
    )


@pytest.mark.parametrize(
    "frame",
    (
        b'{"version":1,"type":"START","nested":{"x":1,"x":2}}\n',
        b'{"version":1,"version":1,"type":"START"}\n',
        b'{"version":1,"type":"START","timeout":NaN}\n',
        b'{"version":1,"type":"START","timeout":1e999}\n',
        b'{"version":true,"type":"START"}\n',
        b'{"version":99,"type":"START"}\n',
        b'{"version":1,"type":"START"}',
        b'{"version":1,"type":"START"}\n\n',
        b'{"version":1,"type":"START"}\n ',
        b'{"version":1}\n',
        b'{"version":1,"type":""}\n',
        b'{"version":1,"type":"START","value":"\\ud800"}\n',
        b'{"version":1,"type":"START","\\udc80":1}\n',
        b'{"version":1,"type":"START","items":["ok","\\udfff"]}\n',
        b'\xff\n',
        b'"scalar"\n',
        b'not-json\n',
        b'{"version":1.0,"type":"START"}\n',
    ),
)
def test_protocol_rejects_invalid_framing_and_json(frame: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_frame(frame)


def test_protocol_size_limit_applies_to_both_encoding_and_decoding() -> None:
    with pytest.raises(ProtocolError):
        encode_frame("TEST", payload="x" * MAX_FRAME_SIZE)
    with pytest.raises(ProtocolError):
        decode_frame(b" " * MAX_FRAME_SIZE + b"\n")


def test_terminal_round_trip_retains_group_causes_notes_and_provenance() -> None:
    cause = OSError("cleanup failed")
    cause.add_note("nested cleanup detail")
    group = ExceptionGroup(
        "independent failures", [cause, ExceptionGroup("nested", [ValueError("bad")])]
    )
    failure = Diagnostic.from_exception("CLEANUP", group)
    snapshot = replace(_terminal(), outcome=_terminal().outcome.with_failure(failure))
    assert decode_terminal(_event("SESSION_ENDED", **terminal_fields(snapshot))) == snapshot


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("trigger", "signal"),
        ("trigger", "unknown"),
        ("child_result", {"returncode": 0, "termination_requested": False, "extra": True}),
        ("child_result", {"returncode": True, "termination_requested": False}),
        ("child_result", None),
        ("child_result", {"returncode": 0, "termination_requested": 0}),
        (
            "cleanup",
            {
                "child_disposal": "not_acquired",
                "workspace_disposal": "confirmed",
                "residual_resources": [],
            },
        ),
        (
            "cleanup",
            {
                "child_disposal": "unconfirmed",
                "workspace_disposal": "confirmed",
                "residual_resources": ["child_group"],
            },
        ),
        ("primary_failure", {"code": "FAIL", "message": "bad", "diagnostics": [], "extra": 1}),
        ("primary_failure", {"code": "", "message": "bad", "diagnostics": []}),
        ("primary_failure", {"code": "FAIL", "message": None, "diagnostics": []}),
        ("primary_failure", {"code": "FAIL", "message": "bad", "diagnostics": {}}),
        (
            "diagnostics",
            [
                {
                    "code": "FAIL",
                    "message": "bad",
                    "diagnostics": [{"code": "", "message": "", "diagnostics": []}],
                }
            ],
        ),
        (
            "cleanup",
            {
                "child_disposal": "released",
                "workspace_disposal": "confirmed",
                "residual_resources": [],
            },
        ),
        (
            "cleanup",
            {
                "child_disposal": "confirmed",
                "workspace_disposal": "removed",
                "residual_resources": [],
            },
        ),
        ("version", True),
    ),
)
def test_terminal_rejects_inconsistent_outcomes_and_cleanup(field: str, value: object) -> None:
    message = _event("SESSION_ENDED", **terminal_fields(_terminal()))
    message[field] = value
    with pytest.raises(ProtocolError):
        decode_terminal(message)


def test_terminal_rejects_unknown_residual_resource() -> None:
    snapshot = TerminalSnapshot(
        Outcome(Trigger.HELPER_FAILURE, Diagnostic("CLEANUP", "group remains")),
        CleanupReport("unconfirmed", "unconfirmed", ("child_group", "workspace")),
    )
    message = _event("SESSION_ENDED", **terminal_fields(snapshot))
    decode_terminal(message)
    # Otherwise consistent: only the residual-name allow-list rejects it.
    message["cleanup"]["residual_resources"] = ["unknown_resource", "workspace"]
    with pytest.raises(ProtocolError):
        decode_terminal(message)


@pytest.mark.parametrize(
    "violation",
    (
        "attempt-before-creation",
        "ready-before-attempt",
        "duplicate-attempt",
        "ready-process-exit",
        "duplicate-ready",
        "attempt-after-ready",
        "unadmitted-output",
        "unadmitted-result",
    ),
)
def test_order_rejects_invalid_attempt_and_ready_histories(violation: str) -> None:
    order = EventOrder(
        CompletionPolicy.PROCESS_EXIT
        if violation == "ready-process-exit"
        else CompletionPolicy.LIVE_SERVER
    )
    if violation != "attempt-before-creation":
        order.accept(
            _event(
                "SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/work"
            )
        )
    ready = _event("READY", remote_address="127.64.0.1", child_pid=1)
    attempt = _event("ATTEMPT", argv=["openocd"])
    if violation not in {
        "attempt-before-creation",
        "ready-before-attempt",
        "unadmitted-output",
        "unadmitted-result",
    }:
        order.accept(attempt)
    if violation in {"duplicate-ready", "attempt-after-ready"}:
        order.accept(ready)
    invalid = ready
    if violation in {"attempt-before-creation", "duplicate-attempt", "attempt-after-ready"}:
        invalid = attempt
    elif violation == "unadmitted-output":
        invalid = _event("CHILD_OUTPUT", stream="stderr", payload="bad", line_end=False)
    elif violation == "unadmitted-result":
        invalid = _event("SESSION_ENDED", **terminal_fields(_terminal()))
    with pytest.raises(ProtocolError):
        order.accept(invalid)


@pytest.mark.parametrize(
    "fields",
    (
        {"remote_address": "127.0.0.1", "child_pid": 1},
        {"remote_address": "127.64.0.0", "child_pid": 1},
        {"remote_address": "127.127.255.255", "child_pid": 1},
        {"remote_address": "localhost", "child_pid": 1},
        {"remote_address": 2134900737, "child_pid": 1},  # 127.64.0.1 as a number
        {"remote_address": "127.64.0.1", "child_pid": 0},
        {"remote_address": "127.64.0.1", "child_pid": True},
    ),
)
def test_order_rejects_invalid_ready_fields(fields: dict[str, object]) -> None:
    order = EventOrder()
    order.accept(
        _event("SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/work")
    )
    order.accept(_event("ATTEMPT", argv=["openocd"]))
    with pytest.raises(ProtocolError):
        order.accept(_event("READY", **fields))
    order.accept(_event("READY", remote_address="127.64.0.1", child_pid=1))


@pytest.mark.parametrize(
    "policy, history, child_disposal, workspace_disposal",
    tuple(
        (policy, history, child_disposal, workspace_disposal)
        for policy in CompletionPolicy
        for history, child_disposal, workspace_disposal in (
            (("SESSION_CREATED",), "not_acquired", "not_created"),
            (("SESSION_CREATED", "ATTEMPT", "READY"), "not_acquired", "confirmed"),
            (("SESSION_CREATED", "ATTEMPT", "CHILD_OUTPUT"), "not_acquired", "confirmed"),
        )
        if "READY" not in history or policy == CompletionPolicy.LIVE_SERVER
    ),
)
def test_terminal_disposal_must_agree_with_observed_history(
    history: tuple[str, ...],
    child_disposal: ChildDisposal,
    workspace_disposal: WorkspaceDisposal,
    policy: CompletionPolicy,
) -> None:
    order = EventOrder(policy)
    events = {
        "SESSION_CREATED": _event(
            "SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/work"
        ),
        "ATTEMPT": _event("ATTEMPT", argv=["openocd"]),
        "READY": _event("READY", remote_address="127.64.0.1", child_pid=1),
        "CHILD_OUTPUT": _event("CHILD_OUTPUT", stream="stdout", payload="started", line_end=True),
    }
    for kind in history:
        order.accept(events[kind])
    snapshot = TerminalSnapshot(
        Outcome(Trigger.CONTROLLER_EOF), CleanupReport(child_disposal, workspace_disposal)
    )
    # The frame is internally consistent; only the preceding history refutes it.
    terminal = _event("SESSION_ENDED", **terminal_fields(snapshot))
    assert decode_terminal(terminal) == snapshot
    with pytest.raises(ProtocolError):
        order.accept(terminal)


@pytest.mark.parametrize(
    "history, child_disposal, workspace_disposal",
    (
        ((), "not_acquired", "not_created"),
        ((), "not_acquired", "confirmed"),
        (("SESSION_CREATED",), "not_acquired", "confirmed"),
        (("SESSION_CREATED", "ATTEMPT"), "not_acquired", "confirmed"),
        (("SESSION_CREATED", "ATTEMPT", "CHILD_OUTPUT"), "confirmed", "confirmed"),
    ),
    ids=(
        "creation-failure",
        "creation-publication-failure",
        "before-start",
        "failed-spawn",
        "child-output",
    ),
)
@pytest.mark.parametrize("policy", tuple(CompletionPolicy))
def test_terminal_history_preserves_partial_startup_and_output_paths(
    history: tuple[str, ...],
    child_disposal: ChildDisposal,
    workspace_disposal: WorkspaceDisposal,
    policy: CompletionPolicy,
) -> None:
    order = EventOrder(policy)
    events = {
        "SESSION_CREATED": _event(
            "SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/work"
        ),
        "ATTEMPT": _event("ATTEMPT", argv=["openocd"]),
        "CHILD_OUTPUT": _event("CHILD_OUTPUT", stream="stdout", payload="started", line_end=True),
    }
    for kind in history:
        order.accept(events[kind])
    snapshot = TerminalSnapshot(
        Outcome(Trigger.HELPER_FAILURE, Diagnostic("FAILED", "controlled failure")),
        CleanupReport(child_disposal, workspace_disposal),
    )
    order.accept(_event("SESSION_ENDED", **terminal_fields(snapshot)))
    assert order.ended


def test_protocol_strings_are_unicode_scalar_text() -> None:
    # An escaped surrogate pair is one scalar value; only isolated halves are invalid.
    assert (
        decode_frame(b'{"version":1,"type":"T","value":"\\ud83d\\ude00"}\n')["value"]
        == "\U0001f600"
    )
    for fields in ({"value": "\udc80"}, {"items": ["ok", "\ud800"]}, {"nested": {"\udfff": 1}}):
        with pytest.raises(ProtocolError):
            encode_frame("T", **fields)


def test_frame_round_trip_accepts_json_whitespace_and_reserves_envelope_fields() -> None:
    frame = encode_frame("HELLO", value=3)
    assert decode_frame(b" \t" + frame[:-1] + b" \t\r\n") == {
        "version": 1,
        "type": "HELLO",
        "value": 3,
    }
    with pytest.raises(ProtocolError):
        encode_frame("HELLO", version=1)


@pytest.mark.parametrize("trigger", (Trigger.CHILD_EXIT, Trigger.HELPER_FAILURE))
def test_terminal_ends_event_history(trigger: Trigger) -> None:
    order = EventOrder()
    order.accept(
        _event("SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/work")
    )
    if trigger == Trigger.CHILD_EXIT:
        order.accept(_event("ATTEMPT", argv=["openocd"]))
        order.accept(
            _event("CHILD_OUTPUT", stream="stdout", payload="before ready", line_end=False)
        )
        order.accept(_event("READY", remote_address="127.64.0.1", child_pid=1))
        for stream in ("stdout", "stderr", "stdout"):
            order.accept(
                _event("CHILD_OUTPUT", stream=stream, payload="after ready", line_end=False)
            )
        snapshot = _terminal()
    else:
        snapshot = TerminalSnapshot(
            Outcome(trigger, Diagnostic("FAILED", "controlled failure")),
            CleanupReport("not_acquired", "confirmed"),
        )
    order.accept(_event("SESSION_ENDED", **terminal_fields(snapshot)))
    for following in (
        _event("CHILD_OUTPUT", stream="stderr", payload="late", line_end=False),
        _event("READY", remote_address="127.64.0.1", child_pid=1),
    ):
        with pytest.raises(ProtocolError):
            order.accept(following)


def _log_history(order: EventOrder) -> None:
    order.accept(
        _event("SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/w")
    )
    order.accept(_event("ATTEMPT", argv=["openocd"]))


def _log_terminal(log: LogReport | None) -> dict[str, Any]:
    snapshot = TerminalSnapshot(
        Outcome(Trigger.CHILD_EXIT, child_result=ChildResult(0, False)),
        CleanupReport("confirmed", "confirmed"),
        log,
    )
    return _event("SESSION_ENDED", **terminal_fields(snapshot))


def test_log_data_preserves_exact_bytes_and_offsets_track_the_received_total() -> None:
    order = EventOrder(CompletionPolicy.PROCESS_EXIT, log_requested=True)
    _log_history(order)
    chunks = (bytes(range(256)), b"\n\r\x00\xff", b"x" * MAX_LOG_DATA_BYTES)
    offset = 0
    received = b""
    for chunk in chunks:
        event = decode_frame(encode_log_data(offset, chunk))
        order.accept(event)
        received += log_data(event)
        offset += len(chunk)
    assert received == b"".join(chunks)
    order.accept(_log_terminal(LogReport(offset, True)))
    assert order.ended


@pytest.mark.parametrize(
    "data",
    (
        "",
        "QQ",
        "QQ=",
        "QR==",
        "Q Q==",
        "QQ==\n",
        "-_8=",
        "QUFB" * ((MAX_LOG_DATA_BYTES // 3) + 1),
        1,
        None,
    ),
    ids=(
        "empty",
        "unpadded",
        "short-padding",
        "non-canonical-bits",
        "whitespace",
        "newline",
        "url-alphabet",
        "oversized",
        "integer",
        "null",
    ),
)
def test_log_data_requires_canonical_standard_base64_of_bounded_size(data: object) -> None:
    order = EventOrder(log_requested=True)
    _log_history(order)
    with pytest.raises(ProtocolError):
        order.accept(_event("LOG_DATA", offset=0, data=data))


@pytest.mark.parametrize(
    ("log_requested", "attempted", "offset"),
    ((False, True, 0), (True, False, 0), (True, True, 1), (True, True, -1), (True, True, True)),
    ids=("not-requested", "before-attempt", "gap", "negative", "boolean"),
)
def test_order_rejects_log_data_outside_its_history(
    log_requested: bool, attempted: bool, offset: object
) -> None:
    order = EventOrder(log_requested=log_requested)
    order.accept(
        _event("SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/w")
    )
    if attempted:
        order.accept(_event("ATTEMPT", argv=["openocd"]))
    with pytest.raises(ProtocolError):
        order.accept(_event("LOG_DATA", offset=offset, data="QQ=="))


@pytest.mark.parametrize(
    ("log_requested", "log"),
    (
        (True, None),
        (False, LogReport(0, True)),
        (True, LogReport(1, True)),
        (True, LogReport(0, False, Diagnostic("LOG_RELAY_OVERFLOW", "overflow"))),
    ),
    ids=("missing-log", "unrequested-log", "count-mismatch", "incomplete-count-mismatch"),
)
def test_terminal_log_must_agree_with_start_and_received_total(
    log_requested: bool, log: LogReport | None
) -> None:
    order = EventOrder(CompletionPolicy.PROCESS_EXIT, log_requested=log_requested)
    _log_history(order)
    if log is not None and log.complete is False:
        order.accept(decode_frame(encode_log_data(0, b"x")))
    with pytest.raises(ProtocolError):
        order.accept(_log_terminal(log))


@pytest.mark.parametrize(
    "log",
    (
        {
            "byte_count": 0,
            "complete": True,
            "diagnostic": {"code": "X", "message": "", "diagnostics": []},
        },
        {"byte_count": 0, "complete": False, "diagnostic": None},
        {"byte_count": -1, "complete": True, "diagnostic": None},
        {"byte_count": 0, "complete": 1, "diagnostic": None},
        {"byte_count": 0, "complete": True},
        {"byte_count": 0, "complete": True, "diagnostic": None, "extra": None},
    ),
    ids=(
        "complete-with-diagnostic",
        "incomplete-without-diagnostic",
        "negative-count",
        "integer-complete",
        "missing-field",
        "unknown-field",
    ),
)
def test_terminal_rejects_invalid_log_report(log: dict[str, object]) -> None:
    fields = terminal_fields(_terminal())
    fields["log"] = log
    with pytest.raises(ProtocolError):
        decode_terminal(_event("SESSION_ENDED", **fields))


def test_complete_log_requires_confirmed_or_no_child_acquisition() -> None:
    fields = terminal_fields(_terminal())
    fields["cleanup"] = {
        "child_disposal": "unconfirmed",
        "workspace_disposal": "unconfirmed",
        "residual_resources": ["child_group", "workspace"],
    }
    fields["primary_failure"] = {"code": "CLEANUP_FAILURE", "message": "", "diagnostics": []}
    fields["log"] = {"byte_count": 0, "complete": True, "diagnostic": None}
    with pytest.raises(ProtocolError):
        decode_terminal(_event("SESSION_ENDED", **fields))


def test_log_data_proves_child_acquisition() -> None:
    order = EventOrder(log_requested=True)
    _log_history(order)
    order.accept(decode_frame(encode_log_data(0, b"x")))
    snapshot = TerminalSnapshot(
        Outcome(Trigger.CONTROLLER_EOF),
        CleanupReport("not_acquired", "confirmed"),
        LogReport(1, True),
    )
    with pytest.raises(ProtocolError):
        order.accept(_event("SESSION_ENDED", **terminal_fields(snapshot)))
