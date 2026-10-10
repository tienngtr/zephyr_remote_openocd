# SPDX-License-Identifier: Apache-2.0

"""Result provenance and failure composition at the pure lifecycle boundary."""

import io

import pytest
from zephyr_remote_openocd.remote.model import RemoteProcess
from zephyr_remote_openocd.remote.outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    Outcome,
    TerminalSnapshot,
    Trigger,
)
from zephyr_remote_openocd.remote.protocol import write_start


def test_established_failure_retains_ordered_nested_diagnostics() -> None:
    primary = Diagnostic("STARTUP", "startup failed")
    cleanup = Diagnostic("CLEANUP", "group disposal failed", (Diagnostic("REAP", "reap failed"),))
    writer = Diagnostic("OUTPUT", "writer failed")
    original = Outcome(Trigger.STARTUP_FAILURE, primary)

    result = original.with_failure(cleanup).with_failure(writer)

    assert result.primary_failure == primary
    assert result.trigger == Trigger.STARTUP_FAILURE
    assert result.diagnostics == (cleanup, writer)
    assert original.diagnostics == ()


def test_exception_boundary_captures_notes_without_mutating_source() -> None:
    error = RuntimeError("cleanup failed")
    error.add_note("descriptor cleanup also failed")
    diagnostic = Diagnostic.from_exception("CLEANUP", error)
    error.add_note("later boundary detail")

    assert diagnostic.message == "cleanup failed"
    assert tuple(detail.message for detail in diagnostic.diagnostics) == (
        "descriptor cleanup also failed",
    )


def test_exception_groups_retain_ordered_nested_causes_and_notes() -> None:
    first = RuntimeError("group disposal failed")
    first.add_note("reap also failed")
    interrupted = KeyboardInterrupt("cleanup interrupted")
    interrupted.add_note("workspace still owned")
    nested = BaseExceptionGroup("independent cleanup", [ValueError("pipe failed"), interrupted])
    nested.add_note("nested cleanup note")
    error = BaseExceptionGroup("session cleanup", [first, nested])
    error.add_note("outer cleanup note")
    error.add_note("transport cleanup also failed")

    diagnostic = Diagnostic.from_exception("CLEANUP", error)
    interrupted.add_note("later detail")

    assert diagnostic == Diagnostic(
        "CLEANUP",
        str(error),
        (
            Diagnostic("CLEANUP", str(first), (Diagnostic("EXCEPTION_NOTE", "reap also failed"),)),
            Diagnostic(
                "CLEANUP",
                str(nested),
                (
                    Diagnostic("CLEANUP", "pipe failed"),
                    Diagnostic(
                        "CLEANUP",
                        str(interrupted),
                        (Diagnostic("EXCEPTION_NOTE", "workspace still owned"),),
                    ),
                    Diagnostic("EXCEPTION_NOTE", "nested cleanup note"),
                ),
            ),
            Diagnostic("EXCEPTION_NOTE", "outer cleanup note"),
            Diagnostic("EXCEPTION_NOTE", "transport cleanup also failed"),
        ),
    )


@pytest.mark.parametrize(
    "trigger",
    [trigger for trigger in Trigger if trigger not in (Trigger.CONTROLLER_EOF, Trigger.CHILD_EXIT)],
)
def test_failure_trigger_cannot_represent_success(trigger: Trigger) -> None:
    with pytest.raises(ValueError):
        Outcome(trigger)
    assert Outcome(trigger, Diagnostic("FAILURE", "failed")).operation_failed(
        CompletionPolicy.LIVE_SERVER
    )


def test_signal_failure_preserves_controller_trigger_and_child_zero() -> None:
    result = Outcome(Trigger.CONTROLLER_EOF, child_result=ChildResult(1, 0, False)).with_failure(
        Diagnostic("SIGNAL", "remote helper interrupted")
    )
    assert result.trigger == Trigger.CONTROLLER_EOF
    assert result.child_result == ChildResult(1, 0, False)
    assert result.operation_failed(CompletionPolicy.PROCESS_EXIT)


@pytest.mark.parametrize(
    ("result", "live_failed", "exit_failed"),
    (
        (None, False, True),
        (ChildResult(1, 0, False), False, False),
        (ChildResult(1, 7, False), True, True),
        (ChildResult(1, -15, True), False, True),
        (ChildResult(1, 0, True), False, True),
    ),
)
def test_child_status_interpretation_uses_operation_policy_and_provenance(
    result: ChildResult | None, live_failed: bool, exit_failed: bool
) -> None:
    outcome = Outcome(Trigger.CONTROLLER_EOF, child_result=result)
    assert outcome.operation_failed(CompletionPolicy.LIVE_SERVER) is live_failed
    assert outcome.operation_failed(CompletionPolicy.PROCESS_EXIT) is exit_failed


def test_child_observation_cannot_be_replaced_with_infrastructure_status() -> None:
    result = ChildResult(2, -15, True)
    outcome = Outcome(Trigger.CONTROLLER_EOF).with_child_result(result)
    failed = outcome.with_failure(Diagnostic("SSH", "SSH exited with status 255"))

    assert failed.child_result == result
    with pytest.raises(ValueError):
        failed.with_child_result(ChildResult(2, 255, True))
    assert Outcome(Trigger.HELPER_FAILURE, Diagnostic("SSH", "transport died")).child_result is None


def test_unconfirmed_disposal_retains_dependencies_and_fails_zero_result() -> None:
    cleanup = CleanupReport("unconfirmed", "unconfirmed", ("child_group", "workspace"))
    outcome = Outcome(Trigger.CONTROLLER_EOF, child_result=ChildResult(1, 0, False))
    with pytest.raises(ValueError):
        TerminalSnapshot(outcome, cleanup)

    snapshot = TerminalSnapshot(
        outcome.with_failure(Diagnostic("CLEANUP", "group remains")), cleanup
    )
    assert snapshot.operation_failed(CompletionPolicy.LIVE_SERVER)
    with pytest.raises(ValueError):
        CleanupReport("unconfirmed", "confirmed", ("child_group",))
    with pytest.raises(ValueError):
        CleanupReport("confirmed", "confirmed", ("child_group",))


def test_terminal_cannot_claim_no_child_after_observed_exit() -> None:
    outcome = Outcome(Trigger.CHILD_EXIT, child_result=ChildResult(1, 0, False))
    with pytest.raises(ValueError):
        TerminalSnapshot(outcome, CleanupReport("not_acquired", "confirmed"))


def test_internal_completion_policy_does_not_change_protocol_v1() -> None:
    frames = []
    for policy in CompletionPolicy:
        process = RemoteProcess(("openocd",), completion_policy=policy)
        stream = io.BytesIO()
        write_start(stream, process, ())
        frames.append(stream.getvalue())
    assert frames[0] == frames[1]
