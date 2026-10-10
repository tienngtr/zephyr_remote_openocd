# SPDX-License-Identifier: Apache-2.0
"""Behavioral checks for the tagged decision sketch's meaningful boundaries."""

from __future__ import annotations

import pytest

from .tagged import (
    Active,
    AttemptDiagnostic,
    Authorized,
    Cancelling,
    ChildResult,
    Closed,
    Command,
    Created,
    Diagnostic,
    Observation,
    Opening,
    Outcome,
    Owned,
    Producing,
    SessionEnded,
    Settled,
    Starting,
    State,
    Terminating,
    decide,
    local_decide,
    local_failure,
    merge_terminal,
)

ARGV = ('openocd', '-c', 'init')
PRIMARY = Diagnostic('operation', 'failed')
CLEANUP = Diagnostic('cleanup', 'close failed', (Diagnostic('nested', 'detail'),))


def test_cancel_ignores_late_ready_and_cannot_launch_client() -> None:
    local, launched = local_decide(Opening(), 'cancel')
    for event in ('ready', 'forwarded', 'launch'):
        local, launched = local_decide(local, event)
        assert isinstance(local, Cancelling) and not launched


@pytest.mark.parametrize('events', (('ready', 'forwarded'), ('forwarded', 'ready')))
def test_launch_needs_both_conditions(events: tuple[str, str]) -> None:
    local, launched = local_decide(Opening(), events[0])
    local, launched = local_decide(local, 'launch')
    assert not launched
    local, launched = local_decide(local, events[1])
    local, launched = local_decide(local, 'launch')
    assert launched


def test_remote_readiness_uses_state_specific_evidence() -> None:
    state = decide(Created(ARGV, frozenset({'init', 'startup'})), Observation('start')).state
    state = decide(state, Observation('dispatch', 1)).state
    state = decide(state, Observation('producer-final', 1, handle='handle')).state
    assert isinstance(state, Starting)
    assert isinstance(decide(state, Observation('ready-admitted', 1)).state, Starting)
    for marker in ('init', 'startup'):
        state = decide(state, Observation('marker', 1, marker=marker)).state
    assert isinstance(decide(state, Observation('ready-admitted', 1)).state, Active)


def test_termination_revokes_authorized_dispatch() -> None:
    state = Starting(Authorized(1, ARGV), frozenset())
    decision = decide(state, Observation('controller-ended'))
    assert isinstance(decision.state, Terminating)
    assert isinstance(decision.state.attempt, Settled)
    assert decide(decision.state, Observation('dispatch', 1)).outputs == ()


def test_pending_producer_cannot_retry_or_close_until_final_response() -> None:
    state = Starting(Producing(1, ARGV), frozenset(), provisional=PRIMARY)
    assert decide(state, Observation('retry', 1)).state is state
    terminating = Terminating(state.attempt, Outcome('timeout', PRIMARY))
    assert decide(terminating, Observation('finalize')).state is terminating
    decision = decide(terminating, Observation('producer-final', 1, handle='late'))
    assert isinstance(decision.state, Terminating)
    assert isinstance(decision.state.attempt, Owned)
    assert decision.state.attempt.custodian == 'producer'
    assert decision.outputs == (Command('cleanup', 1),)


def test_retry_requires_settlement_and_stale_results_are_ignored() -> None:
    state = Starting(Settled(1, ARGV), frozenset(), provisional=PRIMARY)
    newer = decide(state, Observation('retry', 1)).state
    assert isinstance(newer, Starting) and newer.attempt.generation == 2
    for kind in ('producer-final', 'marker', 'child-exit'):
        assert decide(newer, Observation(kind, 1, handle='old')).state is newer


@pytest.mark.parametrize('shutdown_first', (False, True))
def test_shutdown_exit_race_preserves_real_child_result(shutdown_first: bool) -> None:
    state: State = Active(Owned(1, ARGV, 'child'))
    if shutdown_first:
        state = decide(state, Observation('controller-ended')).state
    state = decide(state, Observation('child-exit', 1, child=ChildResult(7))).state
    if not shutdown_first:
        state = decide(state, Observation('controller-ended')).state
    assert isinstance(state, Terminating)
    assert state.outcome.cause == ('controller-ended' if shutdown_first else 'process-exit')
    assert state.outcome.child == ChildResult(7)
    assert (state.outcome.primary is None) == shutdown_first


def test_primary_cleanup_and_post_terminal_writer_failure_are_retained() -> None:
    state: State = Terminating(Owned(1, ARGV, 'child'), Outcome('failure', PRIMARY))
    state = decide(state, Observation('cleanup-final', 1, diagnostic=CLEANUP)).state
    decision = decide(state, Observation('finalize'))
    assert isinstance(decision.state, Closed) and decision.state.residuals == ('child',)
    assert decision.outputs == (SessionEnded(Outcome('failure', PRIMARY, (CLEANUP,))),)
    later = decide(
        decision.state, Observation('output-failed', diagnostic=Diagnostic('writer', 'lost'))
    )
    assert isinstance(later.state, Closed) and later.state.outcome.primary == PRIMARY
    assert later.state.outcome.diagnostics == (CLEANUP, Diagnostic('writer', 'lost'))
    assert later.outputs == ()


def test_attempt_diagnostic_precedes_spawn_command_in_decision() -> None:
    state = Starting(Authorized(1, ARGV), frozenset())
    decision = decide(state, Observation('dispatch', 1))
    assert isinstance(decision.outputs[0], AttemptDiagnostic)
    assert decision.outputs[0].argv == ARGV
    assert decision.outputs[1] == Command('spawn', 1)
    assert isinstance(decision.state, Starting)
    assert isinstance(decision.state.attempt, Producing)


def test_leader_exit_keeps_descendant_cleanup_owner_until_settlement() -> None:
    state: State = Active(Owned(1, ARGV, 'process-group'))
    decision = decide(state, Observation('child-exit', 1, child=ChildResult(7)))
    assert isinstance(decision.state, Terminating)
    assert isinstance(decision.state.attempt, Owned)
    assert decision.outputs == (Command('cleanup', 1),)
    assert decide(decision.state, Observation('finalize')).state is decision.state
    settled = decide(decision.state, Observation('cleanup-final', 1)).state
    assert isinstance(decide(settled, Observation('finalize')).state, Closed)


def test_local_operation_primary_survives_remote_cleanup_and_transport_failure() -> None:
    local = local_failure(Opening(), PRIMARY)
    local = local_failure(local, Diagnostic('transport', 'helper transport status 255'))
    outcome = merge_terminal(local, Outcome('controller-ended', CLEANUP, child=ChildResult(7)))
    assert outcome.primary == PRIMARY
    assert outcome.diagnostics == (Diagnostic('transport', 'helper transport status 255'), CLEANUP)
    assert outcome.child == ChildResult(7)
    no_child = merge_terminal(local, Outcome('controller-ended'))
    assert no_child.child is None


@pytest.mark.parametrize('status', (0, 7))
def test_oneshot_result_needs_no_live_readiness_and_preserves_failure(status: int) -> None:
    state = decide(Created(ARGV, frozenset(), policy='exit'), Observation('start')).state
    state = decide(state, Observation('dispatch', 1)).state
    state = decide(state, Observation('producer-final', 1, handle='group')).state
    assert isinstance(decide(state, Observation('ready-admitted', 1)).state, Starting)
    state = decide(state, Observation('child-exit', 1, child=ChildResult(status))).state
    assert isinstance(state, Terminating)
    assert state.outcome.child == ChildResult(status)
    if status:
        assert state.outcome.primary is not None and state.outcome.primary.source == 'openocd'
        after = decide(state, Observation('cleanup-final', 1, diagnostic=CLEANUP)).state
        assert isinstance(after, Terminating)
        assert after.outcome.primary == state.outcome.primary
        assert after.outcome.diagnostics == (CLEANUP,)
    else:
        assert state.outcome.primary is None
