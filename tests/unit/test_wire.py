# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace

import pytest
from zephyr_remote_openocd.remote.outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    Outcome,
    TerminalSnapshot,
    Trigger,
)
from zephyr_remote_openocd.remote.wire import (
    MAX_FRAME_SIZE,
    EventOrder,
    ProtocolError,
    decode_frame,
    decode_terminal,
    encode_frame,
    terminal_fields,
)


def _event(kind, **fields):
    return decode_frame(encode_frame(kind, **fields))


def _terminal():
    return TerminalSnapshot(
        Outcome(Trigger.CHILD_EXIT, child_result=ChildResult(1, 0, False)),
        CleanupReport("confirmed", "confirmed"),
    )


@pytest.mark.parametrize(
    "frame",
    (
        b'{"version":2,"type":"START","nested":{"x":1,"x":2}}\n',
        b'{"version":2,"version":2,"type":"START"}\n',
        b'{"version":2,"type":"START","timeout":NaN}\n',
        b'{"version":2,"type":"START","timeout":1e999}\n',
        b'{"version":true,"type":"START"}\n',
        b'{"version":1,"type":"START"}\n',
        b'{"version":2,"type":"START"}',
        b'{"version":2,"type":"START"}\n\n',
        b'\xff\n',
        b'"scalar"\n',
    ),
)
def test_v2_rejects_invalid_framing_and_json(frame):
    with pytest.raises(ProtocolError):
        decode_frame(frame)


def test_v2_size_limit_applies_to_both_encoding_and_decoding():
    with pytest.raises(ProtocolError):
        encode_frame("TEST", payload="x" * MAX_FRAME_SIZE)
    with pytest.raises(ProtocolError):
        decode_frame(b" " * MAX_FRAME_SIZE + b"\n")


def test_terminal_round_trip_retains_group_causes_notes_and_provenance():
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
        ("child_result", {"generation": True, "returncode": 0, "termination_requested": False}),
        ("child_result", {"generation": 1, "returncode": True, "termination_requested": False}),
        ("child_result", None),
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
        ("version", True),
    ),
)
def test_terminal_rejects_inconsistent_outcomes_and_cleanup(field, value):
    message = _event("SESSION_ENDED", **terminal_fields(_terminal()))
    message[field] = value
    with pytest.raises(ProtocolError):
        decode_terminal(message)


def test_order_allows_retired_output_but_only_final_child_result():
    order = EventOrder()
    order.accept(
        _event("SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/work")
    )
    for generation in (1, 2):
        order.accept(_event("ATTEMPT", generation=generation, argv=["openocd"]))
    order.accept(
        _event("CHILD_OUTPUT", generation=1, stream="stdout", payload="retired", line_end=False)
    )
    with pytest.raises(ProtocolError):
        order.accept(_event("SESSION_ENDED", **terminal_fields(_terminal())))
    snapshot = replace(
        _terminal(), outcome=Outcome(Trigger.CHILD_EXIT, child_result=ChildResult(2, 0, False))
    )
    order.accept(_event("SESSION_ENDED", **terminal_fields(snapshot)))
    with pytest.raises(ProtocolError):
        order.accept(
            _event("CHILD_OUTPUT", generation=2, stream="stdout", payload="late", line_end=False)
        )


@pytest.mark.parametrize(
    "violation",
    (
        "ready-before-attempt",
        "duplicate-attempt",
        "skipped-attempt",
        "ready-process-exit",
        "duplicate-ready",
        "attempt-after-ready",
        "unadmitted-output",
    ),
)
def test_order_rejects_invalid_attempt_and_ready_histories(violation):
    order = EventOrder(
        CompletionPolicy.PROCESS_EXIT
        if violation == "ready-process-exit"
        else CompletionPolicy.LIVE_SERVER
    )
    order.accept(
        _event("SESSION_CREATED", helper="helper", session_id="session", remote_workspace="/work")
    )
    ready = _event("READY", generation=1, remote_address="127.64.0.1", child_pid=1)
    attempt = _event("ATTEMPT", generation=1, argv=["openocd"])
    if violation != "ready-before-attempt":
        order.accept(attempt)
    if violation in {"duplicate-ready", "attempt-after-ready"}:
        order.accept(ready)
    invalid = ready
    if violation == "duplicate-attempt":
        invalid = attempt
    elif violation in {"skipped-attempt", "attempt-after-ready"}:
        invalid = _event(
            "ATTEMPT", generation=3 if violation == "skipped-attempt" else 2, argv=["openocd"]
        )
    elif violation == "unadmitted-output":
        invalid = _event(
            "CHILD_OUTPUT", generation=2, stream="stderr", payload="bad", line_end=False
        )
    with pytest.raises(ProtocolError):
        order.accept(invalid)
