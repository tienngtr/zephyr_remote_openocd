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
        b'\xff\n',
        b'"scalar"\n',
    ),
)
def test_protocol_rejects_invalid_framing_and_json(frame):
    with pytest.raises(ProtocolError):
        decode_frame(frame)


def test_protocol_size_limit_applies_to_both_encoding_and_decoding():
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
        ("child_result", {"returncode": 0, "termination_requested": False, "extra": True}),
        ("child_result", {"returncode": True, "termination_requested": False}),
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


@pytest.mark.parametrize(
    "violation",
    (
        "ready-before-attempt",
        "duplicate-attempt",
        "ready-process-exit",
        "duplicate-ready",
        "attempt-after-ready",
        "unadmitted-output",
        "unadmitted-result",
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
    ready = _event("READY", remote_address="127.64.0.1", child_pid=1)
    attempt = _event("ATTEMPT", argv=["openocd"])
    if violation not in {"ready-before-attempt", "unadmitted-output", "unadmitted-result"}:
        order.accept(attempt)
    if violation in {"duplicate-ready", "attempt-after-ready"}:
        order.accept(ready)
    invalid = ready
    if violation in {"duplicate-attempt", "attempt-after-ready"}:
        invalid = attempt
    elif violation == "unadmitted-output":
        invalid = _event("CHILD_OUTPUT", stream="stderr", payload="bad", line_end=False)
    elif violation == "unadmitted-result":
        invalid = _event("SESSION_ENDED", **terminal_fields(_terminal()))
    with pytest.raises(ProtocolError):
        order.accept(invalid)


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
    history, child_disposal, workspace_disposal, policy
):
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
    history, child_disposal, workspace_disposal, policy
):
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
