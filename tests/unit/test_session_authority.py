# SPDX-License-Identifier: Apache-2.0

"""Shared local authority: READY cannot override cancellation or recorded fatal facts."""

import threading

import pytest
from zephyr_remote_openocd.controller.authority import SessionAuthority, SessionError
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.protocol.outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    Outcome,
    TerminalSnapshot,
    Trigger,
)

from tests.protocol_support import terminal_snapshot


@pytest.mark.parametrize("fact", ("cancel", "reader", "terminal"))
def test_recorded_fact_prevents_dependent_entry_after_ready(fact: str) -> None:
    authority = SessionAuthority()
    if fact == "cancel":
        authority.cancel()
    elif fact == "reader":
        authority.record_reader_failure(RuntimeError("reader failed"))
    else:
        authority.record_terminal(terminal_snapshot(Trigger.HELPER_FAILURE, code="FAILED"))
    authority.record_ready()
    with pytest.raises(SessionError):
        authority.admit_work((), ())


def test_entry_requires_ready_and_required_forwarding() -> None:
    authority = SessionAuthority()
    forward = ForwardSpec("gdb", 3333, 3333)
    with pytest.raises(SessionError):
        authority.admit_work((forward,), (forward,))
    authority.record_ready()
    with pytest.raises(SessionError):
        authority.admit_work((forward,), ())
    authority.admit_work((forward,), (forward,))


@pytest.mark.timeout(10)
def test_queued_launch_rechecks_fatal_fact_at_execution_entry() -> None:
    authority = SessionAuthority()
    authority.record_ready()
    scheduled = threading.Event()
    execute = threading.Event()
    errors = []
    launched = []

    def queued_launch() -> None:
        scheduled.set()
        assert execute.wait(5)
        try:
            authority.admit_work((), ())
            launched.append(True)
        except SessionError as error:
            errors.append(error)

    thread = threading.Thread(target=queued_launch)
    thread.start()
    assert scheduled.wait(5)
    authority.record_reader_failure(RuntimeError("fatal fact before execution"))
    execute.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors and not launched


def test_reported_terminal_failure_is_not_replayed_during_close() -> None:
    authority = SessionAuthority()
    authority.record_terminal(
        terminal_snapshot(
            Trigger.HELPER_FAILURE,
            code="FAILED",
            message="controlled terminal failure",
            returncode=7,
        )
    )
    failure = authority.snapshot().failure
    assert failure is not None
    assert "controlled terminal failure" in str(failure)
    assert any("7" in note for note in failure.__notes__)
    assert authority.publish_failure() is failure
    assert authority.take_unreported_failure() is None


def test_failure_inspection_does_not_acknowledge_delivery() -> None:
    authority = SessionAuthority()
    authority.record_reader_failure(RuntimeError("reader failed"))
    failure = authority.snapshot().failure
    assert failure is not None
    assert authority.snapshot().failure is failure
    assert authority.take_unreported_failure() is failure
    assert authority.take_unreported_failure() is None


def test_repeated_reader_failure_retains_first_fact_without_reopening_delivery() -> None:
    authority = SessionAuthority()
    first = RuntimeError("first reader failure")
    authority.record_reader_failure(first)
    published = authority.publish_failure()
    assert published is not None and published.__cause__ is first

    authority.record_reader_failure(RuntimeError("later reader failure"))

    snapshot = authority.snapshot()
    assert snapshot.failure is published
    assert len(snapshot.diagnostics) == 1
    assert authority.take_unreported_failure() is None


def test_later_reader_detail_reopens_delivery_with_established_terminal_primary() -> None:
    authority = SessionAuthority()
    authority.record_terminal(terminal_snapshot(Trigger.STARTUP_FAILURE, code="STARTUP_FAILURE"))
    first = authority.publish_failure()
    assert first is not None
    reader = RuntimeError("trailing protocol corruption")
    reader.add_note("retained reader detail")
    authority.record_reader_failure(reader)

    combined = authority.take_unreported_failure()
    assert combined is not None and str(combined) == str(first)
    assert any("trailing protocol corruption" in note for note in combined.__notes__)
    assert any("retained reader detail" in note for note in combined.__notes__)
    assert authority.take_unreported_failure() is None


def _record_failure(authority: SessionAuthority, source: str, error: BaseException) -> None:
    if source == "child":
        authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=7))
    elif source == "terminal":
        authority.record_terminal(terminal_snapshot(Trigger.HELPER_FAILURE, code="HELPER_FAILURE"))
    elif source == "reader":
        authority.record_reader_failure(error)
    elif source == "transport":
        authority.record_control_status(17)
    else:
        authority.record_failure("OPERATION_FAILURE", error)


@pytest.mark.parametrize(
    "source, code",
    (
        ("child", "TERMINAL_FAILURE"),
        ("terminal", "TERMINAL_FAILURE"),
        ("reader", "READER_FAILURE"),
        ("transport", "CONTROL_PROCESS_FAILURE"),
        ("operation", "OPERATION_FAILURE"),
    ),
)
def test_failure_adapters_record_fatal_facts(source: str, code: str) -> None:
    authority = SessionAuthority()
    error = RuntimeError("operation failed")
    _record_failure(authority, source, error)

    snapshot = authority.snapshot()
    assert snapshot.failure is not None
    assert [detail.code for detail in snapshot.diagnostics] == [code]
    if source == "operation":
        assert snapshot.failure is error
    elif source == "reader":
        assert snapshot.failure.__cause__ is error
    elif source == "transport":
        assert "17" in str(snapshot.failure)
    else:
        assert snapshot.terminal is not None
        if source == "child":
            result = snapshot.terminal.outcome.child_result
            assert result is not None and result.returncode == 7
        else:
            failure = snapshot.terminal.outcome.primary_failure
            assert failure is not None and failure.code == "HELPER_FAILURE"


@pytest.mark.parametrize(
    "first, second",
    (("child", "operation"), ("operation", "transport"), ("transport", "reader")),
)
def test_first_failure_stays_primary_across_publication(first: str, second: str) -> None:
    """Publish one failure, recognize another, then project status and enter."""
    authority = SessionAuthority()
    authority.record_ready()
    later = RuntimeError(f"{second} failure")
    later.add_note("nested secondary diagnostic")

    _record_failure(authority, first, RuntimeError(f"{first} failure"))
    with pytest.raises(RuntimeError) as initial:
        authority.operation_status()
    _record_failure(authority, second, later)
    with pytest.raises(RuntimeError) as status:
        authority.operation_status()
    with pytest.raises(RuntimeError) as launch:
        authority.admit_work((), ())
    assert status.value is initial.value is launch.value
    snapshot = authority.snapshot()
    assert len(snapshot.diagnostics) == 2
    if second in ("reader", "operation"):
        assert any("nested secondary diagnostic" in note for note in status.value.__notes__)
    if "child" in (first, second):
        assert snapshot.terminal is not None
        assert snapshot.terminal.outcome.child_result is not None
        assert snapshot.terminal.outcome.child_result.returncode == 7
    notes = tuple(status.value.__notes__)
    with pytest.raises(RuntimeError) as repeated:
        authority.operation_status()
    assert repeated.value is initial.value
    assert tuple(repeated.value.__notes__) == notes


def test_control_observation_defers_successful_result_until_final_accounting() -> None:
    authority = SessionAuthority()
    authority.set_policy(CompletionPolicy.PROCESS_EXIT)
    authority.begin_control_exit()
    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))

    assert authority.operation_status() is None

    authority.finish_control_exit()

    assert authority.operation_status() == 0


def _admit(authority: SessionAuthority, entry: str) -> None:
    if entry == "work":
        authority.admit_work((), ())
    else:
        authority.admit_helper_effect("START", lambda: None)


@pytest.mark.parametrize("entry", ("work", "start"))
@pytest.mark.parametrize("accounting", ("after-resolution", "before-resolution"))
@pytest.mark.parametrize(
    ("ending", "selected"),
    (
        ("terminal-failure", ["TERMINAL_FAILURE", "OPERATION_FAILURE"]),
        ("orderly-terminal", ["OPERATION_FAILURE"]),
        ("no-terminal", ["CONTROL_ENDED", "OPERATION_FAILURE"]),
    ),
)
def test_refusal_during_final_observation_waits_for_final_accounting(
    entry: str, accounting: str, ending: str, selected: list[str]
) -> None:
    authority = SessionAuthority()
    authority.record_ready()
    authority.begin_control_exit()
    with pytest.raises(SessionError) as refused:
        _admit(authority, entry)

    def account() -> None:
        if ending == "terminal-failure":
            authority.record_terminal(
                terminal_snapshot(Trigger.HELPER_FAILURE, code="DETAIL", message="final detail")
            )
        elif ending == "orderly-terminal":
            authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))
        authority.finish_control_exit()

    if accounting == "before-resolution":
        account()
    resolved = authority.resolve_effect_failure(refused.value)
    if accounting == "after-resolution":
        # The refusal is propagated but not selected while accounting is pending.
        assert resolved is refused.value
        assert authority.snapshot().failure is None
        account()

    snapshot = authority.snapshot()
    assert [diagnostic.code for diagnostic in snapshot.diagnostics] == selected
    assert (snapshot.failure is refused.value) == (ending == "orderly-terminal")
    with pytest.raises(SessionError):
        _admit(authority, entry)


@pytest.mark.parametrize("child_first", (True, False), ids=("child-exit-first", "loss-first"))
def test_terminal_failure_reports_the_helper_established_primary(child_first: bool) -> None:
    loss = Diagnostic("CONTROLLER_LEASE_LOST", "controller input ended without RELEASE")
    result = ChildResult(3, False)
    outcome = (
        Outcome(Trigger.CHILD_EXIT, child_result=result, diagnostics=(loss,))
        if child_first
        else Outcome(Trigger.CONTROLLER_EOF, loss, child_result=result)
    )
    authority = SessionAuthority()
    authority.record_terminal(TerminalSnapshot(outcome, CleanupReport("confirmed", "confirmed")))
    with pytest.raises(SessionError) as raised:
        authority.operation_status()
    main, notes = str(raised.value), " ".join(raised.value.__notes__)
    first, second = ("status 3", "CONTROLLER_LEASE_LOST")
    if not child_first:
        first, second = second, first
    assert first in main and first not in notes
    assert second in notes and second not in main
