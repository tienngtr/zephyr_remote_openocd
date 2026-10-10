# SPDX-License-Identifier: Apache-2.0

"""Semantic transitions on real lifecycle objects, without physical adapters."""

import pytest
from zephyr_remote_openocd.remote.lifecycle import (
    Active,
    Closed,
    LifecycleError,
    RemoteLifecycle,
    Starting,
    Terminating,
)
from zephyr_remote_openocd.remote.model import RemoteProcess
from zephyr_remote_openocd.remote.outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    Trigger,
)

CONFIRMED = CleanupReport("confirmed", "confirmed")
BIND_COLLISION = Diagnostic("BIND_COLLISION", "address already in use")


def _starting(
    *, markers: tuple[str, ...] = (), policy: CompletionPolicy = CompletionPolicy.LIVE_SERVER
) -> tuple[RemoteLifecycle[RemoteProcess], int]:
    lifecycle = RemoteLifecycle[RemoteProcess]()
    generation = lifecycle.start(
        RemoteProcess(("openocd",), required_output_sentinels=markers, completion_policy=policy)
    )
    return lifecycle, generation


def _owned(
    *, markers: tuple[str, ...] = (), policy: CompletionPolicy = CompletionPolicy.LIVE_SERVER
) -> tuple[RemoteLifecycle[RemoteProcess], int]:
    lifecycle, generation = _starting(markers=markers, policy=policy)
    assert lifecycle.enter_attempt(generation, admitted=True)
    assert lifecycle.adopt_attempt(generation)
    return lifecycle, generation


def test_live_readiness_requires_owned_current_live_child_and_evidence() -> None:
    lifecycle, generation = _starting(markers=("init", "complete"))
    assert not lifecycle.ready(generation, child_live=True, admitted=True)
    assert lifecycle.enter_attempt(generation, admitted=True)
    assert not lifecycle.ready(generation, child_live=True, admitted=True)
    assert lifecycle.adopt_attempt(generation)
    assert lifecycle.observe_marker(generation, "init")
    assert not lifecycle.ready(generation, child_live=True, admitted=True)
    assert lifecycle.observe_marker(generation, "complete")
    assert not lifecycle.ready(generation, child_live=False, admitted=True)
    assert lifecycle.ready(generation, child_live=True, admitted=True)
    assert isinstance(lifecycle.state, Active)


def test_empty_marker_live_policy_still_requires_ready() -> None:
    lifecycle, generation = _owned()
    before_ready = lifecycle.state
    assert isinstance(before_ready, Starting)
    assert lifecycle.ready(generation, child_live=True, admitted=True)
    assert isinstance(lifecycle.state, Active)


@pytest.mark.parametrize("boundary", ("attempt", "ready"))
def test_failed_admission_terminates_without_entry_or_activation(boundary: str) -> None:
    lifecycle, generation = _starting()
    if boundary == "attempt":
        assert not lifecycle.enter_attempt(generation, admitted=False)
        assert not lifecycle.adopt_attempt(generation)
    else:
        assert lifecycle.enter_attempt(generation, admitted=True)
        assert lifecycle.adopt_attempt(generation)
        assert not lifecycle.ready(generation, child_live=True, admitted=False)
    state = lifecycle.state
    assert isinstance(state, Terminating)
    assert state.outcome.trigger == Trigger.OUTPUT_FAILURE
    assert state.outcome.primary_failure is not None


@pytest.mark.parametrize("exit_before_adoption", (False, True))
@pytest.mark.parametrize("returncode", (0, 7))
def test_one_shot_exit_is_result_without_readiness_or_retry(
    exit_before_adoption: bool, returncode: int
) -> None:
    lifecycle, generation = _starting(
        markers=("never printed",), policy=CompletionPolicy.PROCESS_EXIT
    )
    assert lifecycle.enter_attempt(generation, admitted=True)
    if exit_before_adoption:
        assert lifecycle.observe_child_exit(generation, returncode)
    assert lifecycle.adopt_attempt(generation)
    assert not lifecycle.ready(generation, child_live=True, admitted=True)
    if not exit_before_adoption:
        assert isinstance(lifecycle.state, Active)
        assert lifecycle.observe_child_exit(generation, returncode)
    assert lifecycle.settle_attempt(generation, producer_quiescent=True, resources_disposed=True)
    assert lifecycle.retry(generation) is None
    snapshot = lifecycle.freeze(CONFIRMED)
    assert snapshot.outcome.trigger == Trigger.CHILD_EXIT
    assert snapshot.outcome.child_result == ChildResult(generation, returncode, False)
    assert snapshot.operation_failed(CompletionPolicy.PROCESS_EXIT) is (returncode != 0)


@pytest.mark.parametrize(
    ("producer_quiescent", "resources_disposed"), ((False, True), (True, False), (False, False))
)
def test_retry_requires_both_producer_quiescence_and_resource_disposal(
    producer_quiescent: bool, resources_disposed: bool
) -> None:
    lifecycle, generation = _owned()
    assert lifecycle.observe_child_exit(generation, 1)
    assert lifecycle.classify_startup_failure(generation, BIND_COLLISION, safely_repeatable=True)
    assert not lifecycle.settle_attempt(
        generation, producer_quiescent=producer_quiescent, resources_disposed=resources_disposed
    )
    assert lifecycle.retry(generation) is None
    assert lifecycle.settle_attempt(generation, producer_quiescent=True, resources_disposed=True)
    assert lifecycle.retry(generation) == generation + 1


def test_retry_discards_old_evidence_and_rejects_stale_attempt_facts() -> None:
    lifecycle, old_generation = _owned(markers=("complete",))
    assert lifecycle.observe_marker(old_generation, "complete")
    assert lifecycle.observe_child_exit(old_generation, 1)
    assert lifecycle.classify_startup_failure(
        old_generation, BIND_COLLISION, safely_repeatable=True
    )
    assert lifecycle.settle_attempt(
        old_generation, producer_quiescent=True, resources_disposed=True
    )
    generation = lifecycle.retry(old_generation)
    assert generation is not None
    assert lifecycle.enter_attempt(generation, admitted=True)
    assert lifecycle.adopt_attempt(generation)
    current = lifecycle.state

    assert not lifecycle.observe_marker(old_generation, "complete")
    assert not lifecycle.observe_child_exit(old_generation, 0)
    assert not lifecycle.adopt_attempt(old_generation)
    assert not lifecycle.settle_attempt(
        old_generation, producer_quiescent=True, resources_disposed=True
    )
    assert not lifecycle.classify_startup_failure(
        old_generation, BIND_COLLISION, safely_repeatable=True
    )
    assert lifecycle.retry(old_generation) is None
    assert lifecycle.state == current
    assert not lifecycle.ready(generation, child_live=True, admitted=True)
    assert lifecycle.observe_marker(generation, "complete")
    assert lifecycle.ready(generation, child_live=True, admitted=True)


def test_unsafe_startup_failure_is_primary_before_cleanup() -> None:
    lifecycle, generation = _owned()
    startup = Diagnostic("STARTUP", "configuration failed")
    assert lifecycle.classify_startup_failure(generation, startup, safely_repeatable=False)
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    lifecycle.terminate(Trigger.SIGNAL, Diagnostic("SIGNAL", "interrupted"))
    assert lifecycle.retry(generation) is None
    state = lifecycle.state
    assert isinstance(state, Terminating)
    assert state.outcome.trigger == Trigger.STARTUP_FAILURE
    assert state.outcome.primary_failure == startup
    assert tuple(detail.code for detail in state.outcome.diagnostics) == ("SIGNAL",)


def test_retry_generation_budget_is_finite() -> None:
    lifecycle, generation = _starting()
    for expected in range(1, 33):
        assert generation == expected
        assert lifecycle.enter_attempt(generation, admitted=True)
        assert lifecycle.adopt_attempt(generation)
        assert lifecycle.observe_child_exit(generation, 1)
        assert lifecycle.classify_startup_failure(
            generation, BIND_COLLISION, safely_repeatable=True
        )
        assert lifecycle.settle_attempt(
            generation, producer_quiescent=True, resources_disposed=True
        )
        next_generation = lifecycle.retry(generation)
        if expected < 32:
            assert next_generation is not None
            generation = next_generation
        else:
            assert next_generation is None
    assert lifecycle.freeze(CONFIRMED).outcome.primary_failure == BIND_COLLISION


def test_termination_blocks_authorized_entry_and_late_ready() -> None:
    lifecycle, generation = _starting()
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    assert not lifecycle.enter_attempt(generation, admitted=True)
    assert not lifecycle.ready(generation, child_live=True, admitted=True)
    assert lifecycle.retry(generation) is None
    with pytest.raises(LifecycleError):
        lifecycle.start(RemoteProcess(("openocd",)))


@pytest.mark.parametrize("exit_before_signal", (False, True))
def test_late_acquisition_stays_in_termination_and_retains_child_provenance(
    exit_before_signal: bool,
) -> None:
    lifecycle, generation = _starting()
    assert lifecycle.enter_attempt(generation, admitted=True)
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    if exit_before_signal:
        assert lifecycle.observe_child_exit(generation, 7)
        assert not lifecycle.record_child_termination(generation)
    else:
        assert lifecycle.record_child_termination(generation)
        assert lifecycle.observe_child_exit(generation, -15)
    assert lifecycle.adopt_attempt(generation)
    assert isinstance(lifecycle.state, Terminating)
    assert not lifecycle.ready(generation, child_live=True, admitted=True)
    assert lifecycle.settle_attempt(generation, producer_quiescent=True, resources_disposed=True)
    snapshot = lifecycle.freeze(CONFIRMED)
    assert snapshot.outcome.trigger == Trigger.CONTROLLER_EOF
    assert snapshot.outcome.child_result == ChildResult(
        generation, 7 if exit_before_signal else -15, not exit_before_signal
    )


def test_unsettled_producer_cannot_claim_disposal_or_lose_residual_responsibility() -> None:
    lifecycle, generation = _starting()
    assert lifecycle.enter_attempt(generation, admitted=True)
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
    lifecycle, generation = _owned()
    lifecycle.terminate(Trigger.CONTROLLER_EOF)
    assert lifecycle.observe_child_exit(generation, 0)
    assert lifecycle.settle_attempt(generation, producer_quiescent=True, resources_disposed=True)
    snapshot = lifecycle.freeze(CONFIRMED)
    signal_failure = Diagnostic("SIGNAL", "helper interrupted after freeze")
    lifecycle.terminate(Trigger.SIGNAL, signal_failure)
    assert lifecycle.freeze(CONFIRMED) is snapshot
    assert snapshot.outcome.primary_failure is None
    state = lifecycle.state
    assert isinstance(state, Closed)
    assert state.local_diagnostics == (signal_failure,)
