# SPDX-License-Identifier: Apache-2.0

"""Semantic transitions on real lifecycle objects, without physical adapters."""

import pytest
from zephyr_remote_openocd.protocol.outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    Trigger,
)
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.remote.lifecycle import (
    Active,
    Closed,
    LifecycleError,
    RemoteLifecycle,
    Starting,
    Terminating,
)

CONFIRMED = CleanupReport("confirmed", "confirmed")


def _starting(
    *, markers: tuple[str, ...] = (), policy: CompletionPolicy = CompletionPolicy.LIVE_SERVER
) -> RemoteLifecycle[ProcessSpec]:
    lifecycle = RemoteLifecycle[ProcessSpec]()
    lifecycle.start(
        ProcessSpec(("openocd",), required_output_sentinels=markers, completion_policy=policy)
    )
    return lifecycle


def _owned(
    *, markers: tuple[str, ...] = (), policy: CompletionPolicy = CompletionPolicy.LIVE_SERVER
) -> RemoteLifecycle[ProcessSpec]:
    lifecycle = _starting(markers=markers, policy=policy)
    assert lifecycle.enter_attempt(admitted=True)
    assert lifecycle.adopt_attempt()
    return lifecycle


def test_live_readiness_requires_owned_current_live_child_and_evidence() -> None:
    lifecycle = _starting(markers=("init", "complete"))
    assert not lifecycle.ready(child_live=True, admitted=True)
    assert lifecycle.enter_attempt(admitted=True)
    assert not lifecycle.ready(child_live=True, admitted=True)
    assert lifecycle.adopt_attempt()
    assert lifecycle.observe_marker("init")
    assert not lifecycle.ready(child_live=True, admitted=True)
    assert lifecycle.observe_marker("complete")
    assert not lifecycle.ready(child_live=False, admitted=True)
    assert lifecycle.ready(child_live=True, admitted=True)
    assert isinstance(lifecycle.state, Active)


def test_empty_marker_live_policy_still_requires_ready() -> None:
    lifecycle = _owned()
    before_ready = lifecycle.state
    assert isinstance(before_ready, Starting)
    assert lifecycle.ready(child_live=True, admitted=True)
    assert isinstance(lifecycle.state, Active)


@pytest.mark.parametrize("boundary", ("attempt", "ready"))
def test_failed_admission_terminates_without_entry_or_activation(boundary: str) -> None:
    lifecycle = _starting()
    if boundary == "attempt":
        assert not lifecycle.enter_attempt(admitted=False)
        assert not lifecycle.adopt_attempt()
    else:
        assert lifecycle.enter_attempt(admitted=True)
        assert lifecycle.adopt_attempt()
        assert not lifecycle.ready(child_live=True, admitted=False)
    state = lifecycle.state
    assert isinstance(state, Terminating)
    assert state.outcome.trigger == Trigger.OUTPUT_FAILURE
    assert state.outcome.primary_failure is not None


@pytest.mark.parametrize("exit_before_adoption", (False, True))
@pytest.mark.parametrize("returncode", (0, 7))
def test_one_shot_exit_is_result_without_readiness(
    exit_before_adoption: bool, returncode: int
) -> None:
    lifecycle = _starting(markers=("never printed",), policy=CompletionPolicy.PROCESS_EXIT)
    assert lifecycle.enter_attempt(admitted=True)
    if exit_before_adoption:
        assert lifecycle.observe_child_exit(returncode)
    assert lifecycle.adopt_attempt()
    assert not lifecycle.ready(child_live=True, admitted=True)
    if not exit_before_adoption:
        assert isinstance(lifecycle.state, Active)
        assert lifecycle.observe_child_exit(returncode)
    assert lifecycle.settle_attempt(producer_quiescent=True, resources_disposed=True)
    snapshot = lifecycle.freeze(CONFIRMED)
    assert snapshot.outcome.trigger == Trigger.CHILD_EXIT
    assert snapshot.outcome.child_result == ChildResult(returncode, False)
    assert snapshot.operation_failed(CompletionPolicy.PROCESS_EXIT) is (returncode != 0)


@pytest.mark.parametrize(
    ("producer_quiescent", "resources_disposed"), ((False, True), (True, False), (False, False))
)
def test_settlement_requires_both_producer_quiescence_and_resource_disposal(
    producer_quiescent: bool, resources_disposed: bool
) -> None:
    lifecycle = _owned()
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    assert lifecycle.observe_child_exit(1)
    assert not lifecycle.settle_attempt(
        producer_quiescent=producer_quiescent, resources_disposed=resources_disposed
    )
    assert lifecycle.settle_attempt(producer_quiescent=True, resources_disposed=True)
    assert lifecycle.freeze(CONFIRMED).cleanup.child_disposal == "confirmed"


def test_startup_failure_is_primary_before_cleanup() -> None:
    lifecycle = _owned()
    startup = Diagnostic("STARTUP", "configuration failed")
    lifecycle.terminate(Trigger.STARTUP_FAILURE, startup)
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    lifecycle.terminate(Trigger.SIGNAL, Diagnostic("SIGNAL", "interrupted"))
    state = lifecycle.state
    assert isinstance(state, Terminating)
    assert state.outcome.trigger == Trigger.STARTUP_FAILURE
    assert state.outcome.primary_failure == startup
    assert tuple(detail.code for detail in state.outcome.diagnostics) == ("SIGNAL",)


def test_termination_blocks_authorized_entry_and_late_ready() -> None:
    lifecycle = _starting()
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    assert not lifecycle.enter_attempt(admitted=True)
    assert not lifecycle.ready(child_live=True, admitted=True)
    with pytest.raises(LifecycleError):
        lifecycle.start(ProcessSpec(("openocd",)))


@pytest.mark.parametrize("exit_before_signal", (False, True))
def test_late_acquisition_stays_in_termination_and_retains_child_provenance(
    exit_before_signal: bool,
) -> None:
    lifecycle = _starting()
    assert lifecycle.enter_attempt(admitted=True)
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    if exit_before_signal:
        assert lifecycle.observe_child_exit(7)
        assert not lifecycle.record_child_termination()
    else:
        assert lifecycle.record_child_termination()
        assert lifecycle.observe_child_exit(-15)
    assert lifecycle.adopt_attempt()
    assert isinstance(lifecycle.state, Terminating)
    assert not lifecycle.ready(child_live=True, admitted=True)
    assert lifecycle.settle_attempt(producer_quiescent=True, resources_disposed=True)
    snapshot = lifecycle.freeze(CONFIRMED)
    assert snapshot.outcome.trigger == Trigger.CONTROLLER_EOF
    assert snapshot.outcome.child_result == ChildResult(
        7 if exit_before_signal else -15, not exit_before_signal
    )


def test_unsettled_producer_cannot_claim_disposal_or_lose_residual_responsibility() -> None:
    lifecycle = _starting()
    assert lifecycle.enter_attempt(admitted=True)
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    with pytest.raises(LifecycleError):
        lifecycle.freeze(CONFIRMED)
    lifecycle.terminate(Trigger.HELPER_FAILURE, Diagnostic("CLEANUP", "producer still running"))
    snapshot = lifecycle.freeze(
        CleanupReport("unconfirmed", "unconfirmed", ("child_producer", "workspace"))
    )
    assert snapshot.outcome.child_result is None
    assert snapshot.operation_failed(CompletionPolicy.LIVE_SERVER)


def test_terminal_is_unique_and_late_signal_stays_outside_frozen_snapshot() -> None:
    lifecycle = _owned()
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    assert lifecycle.observe_child_exit(0)
    assert lifecycle.settle_attempt(producer_quiescent=True, resources_disposed=True)
    snapshot = lifecycle.freeze(CONFIRMED)
    signal_failure = Diagnostic("SIGNAL", "helper interrupted after freeze")
    lifecycle.terminate(Trigger.SIGNAL, signal_failure)
    assert lifecycle.freeze(CONFIRMED) is snapshot
    assert snapshot.outcome.primary_failure is None
    state = lifecycle.state
    assert isinstance(state, Closed)
    assert state.local_diagnostics == (signal_failure,)
