# SPDX-License-Identifier: Apache-2.0
"""Deterministic histories and bounded arbitrary interleavings of the model."""

from dataclasses import replace

import pytest

from experiments.lifecycle import model
from experiments.lifecycle.model import (
    Acquire,
    Acquired,
    Adopt,
    ChildExited,
    CleanupFinished,
    Command,
    CommitReady,
    CommitRetry,
    ControlBatch,
    ControlFailed,
    Effect,
    Event,
    Failure,
    Output,
    Owner,
    Phase,
    ReadinessCandidate,
    ResourceKey,
    Stage,
    StartupFailed,
    StartupTimeout,
    State,
    Supervisor,
    transition,
)

CHILD = ResourceKey(1, 'child')
FORWARD_A = ResourceKey(1, 'forward-a')
FORWARD_B = ResourceKey(1, 'forward-b')
SOCKET = ResourceKey(1, 'rtt')
OPERATIONAL = Failure('OPERATIONAL', 'startup failed')
NESTED = Failure('CLEANUP', 'batch failed', (Failure('CLOSE', 'stream failed'),))
OTHER_CLEANUP = Failure('CLEANUP', 'lock failed')


def run(*events: Event) -> Supervisor:
    supervisor = Supervisor()
    for event in events:
        before = supervisor.state
        after = supervisor.accept(event)
        assert_invariants(before, after)
    return supervisor


def outputs(state: State) -> tuple[Output, ...]:
    return tuple(item for item in state.outbox if isinstance(item, Output))


def kinds(state: State) -> tuple[str, ...]:
    return tuple(output.kind for output in outputs(state))


def effects(state: State, kind: str) -> tuple[Effect, ...]:
    return tuple(item for item in state.outbox if isinstance(item, Effect) and item.kind == kind)


def assert_phase(supervisor: Supervisor, phase: Phase) -> None:
    """Read a fresh snapshot; earlier assertions must not narrow mutable state."""
    assert supervisor.state.phase == phase


def spawned() -> tuple[Event, ...]:
    return (Command.START, Acquired(CHILD), Adopt(CHILD))


def assert_invariants(before: State, after: State) -> None:
    assert after.generation in (before.generation, before.generation + 1)
    assert after.outbox[: len(before.outbox)] == before.outbox
    assert len({resource.key for resource in after.resources}) == len(after.resources)
    if before.outcome is not None:
        assert after.outcome is not None
        assert after.generation == before.generation
        assert after.phase not in (Phase.STARTING, Phase.ACTIVE)
        assert after.outcome.reason == before.outcome.reason
        if before.outcome.primary is not None:
            assert after.outcome.primary == before.outcome.primary
        assert (
            after.outcome.diagnostics[: len(before.outcome.diagnostics)]
            == before.outcome.diagnostics
        )
    if after.generation > before.generation:
        assert before.outcome is None
        assert all(resource.stage == Stage.DONE for resource in before.resources)
    if after.phase == Phase.CLOSED:
        assert after.outcome is not None
        assert all(resource.stage == Stage.DONE for resource in after.resources)
    if before.phase == Phase.CLOSED:
        assert after == before
    cleaned = effects(after, 'CLEAN')
    assert len({effect.key for effect in cleaned}) == len(cleaned)
    for resource in after.resources:
        if resource.stage in (Stage.PENDING, Stage.OFFERED):
            assert resource.owner == Owner.EFFECT
        if resource.stage == Stage.HELD:
            assert resource.owner == Owner.SESSION
        previous = next((item for item in before.resources if item.key == resource.key), None)
        if previous is not None:
            assert resource.stage.value >= previous.stage.value
            if previous.owner == Owner.SESSION:
                assert resource.owner == Owner.SESSION
    protocol = outputs(after)
    ready = tuple(output for output in protocol if output.kind == 'PROCESS_READY')
    assert len(ready) <= 1
    terminal = tuple(output for output in protocol if output.kind in ('ERROR', 'SESSION_CLOSED'))
    assert len(terminal) <= 1
    if terminal:
        assert protocol[-1] == terminal[0]
        assert after.phase == Phase.CLOSED
    for output in after.outbox[len(before.outbox) :]:
        if isinstance(output, Output) and output.kind == 'PROCESS_READY':
            assert before.outcome is None
            assert before.phase == Phase.STARTING and before.ready_candidate
            assert after.phase == Phase.ACTIVE
        if isinstance(output, Output) and output.kind == 'PROCESS_STARTING':
            assert before.outcome is None
            assert output.generation == after.generation


@pytest.mark.parametrize(
    'history',
    (
        (ReadinessCandidate(1), Command.STOP),
        (Command.STOP, ReadinessCandidate(1)),
    ),
    ids=('candidate-then-stop', 'stop-then-candidate'),
)
def test_pending_startup_stop_forbids_ready(history: tuple[Event, ...]) -> None:
    supervisor = run(*spawned(), *history, CommitReady(1))
    assert_phase(supervisor, Phase.RETIRING)
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING')
    supervisor.accept(Command.CLOSE)
    supervisor.accept(CleanupFinished(CHILD))
    assert_phase(supervisor, Phase.CLOSED)
    assert supervisor.state.outcome == model.Outcome('requested')
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'SESSION_CLOSED')


def test_ready_commit_before_stop_is_observable() -> None:
    supervisor = run(*spawned(), ReadinessCandidate(1), CommitReady(1))
    assert_phase(supervisor, Phase.ACTIVE)
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'PROCESS_READY')
    supervisor.accept(Command.STOP)
    supervisor.accept(Command.CLOSE)
    supervisor.accept(CleanupFinished(CHILD))
    assert_phase(supervisor, Phase.CLOSED)
    assert kinds(supervisor.state) == (
        'SESSION_CREATED',
        'PROCESS_STARTING',
        'PROCESS_READY',
        'SESSION_CLOSED',
    )


def test_retryable_failure_then_stop_forbids_retry() -> None:
    supervisor = run(
        *spawned(),
        ChildExited(1, 1, bind_collision=True),
        Command.CLOSE,
        CleanupFinished(CHILD),
        Command.STOP,
        CommitRetry(1),
        Command.CLOSE,
    )
    assert_phase(supervisor, Phase.CLOSED)
    assert supervisor.state.generation == 1
    assert supervisor.state.outcome == model.Outcome('requested')
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'SESSION_CLOSED')


def test_retired_attempt_facts_are_fenced_before_and_after_retry() -> None:
    supervisor = run(
        *spawned(),
        ChildExited(1, 1, bind_collision=True),
        Command.CLOSE,
        CleanupFinished(CHILD),
    )
    retired = supervisor.state
    assert supervisor.accept(ReadinessCandidate(1)) == retired
    assert supervisor.accept(ChildExited(1, 99)) == retired
    supervisor.accept(CommitRetry(1))
    assert_phase(supervisor, Phase.STARTING)
    assert supervisor.state.generation == 2
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'PROCESS_STARTING')
    current = supervisor.state
    for event in (ReadinessCandidate(1), CommitReady(1), ChildExited(1, 99), StartupTimeout(1)):
        assert supervisor.accept(event) == current
    child_two = ResourceKey(2, 'child')
    supervisor.accept(Acquired(child_two))
    supervisor.accept(Adopt(child_two))
    supervisor.accept(ReadinessCandidate(2))
    supervisor.accept(CommitReady(2))
    assert_phase(supervisor, Phase.ACTIVE)
    assert outputs(supervisor.state)[-1] == Output('PROCESS_READY', 2)


def test_primary_failure_survives_multiple_nested_cleanup_failures() -> None:
    supervisor = run(
        *spawned(),
        Acquire(FORWARD_A),
        Acquired(FORWARD_A),
        Adopt(FORWARD_A),
        StartupFailed(1, OPERATIONAL),
        Command.CLOSE,
        CleanupFinished(CHILD, NESTED),
        CleanupFinished(FORWARD_A, OTHER_CLEANUP),
    )
    assert_phase(supervisor, Phase.CLOSED)
    assert supervisor.state.outcome == model.Outcome(
        'failure',
        primary=OPERATIONAL,
        diagnostics=(NESTED, OTHER_CLEANUP),
    )
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'ERROR')
    assert outputs(supervisor.state)[-1].outcome == supervisor.state.outcome


@pytest.mark.parametrize('adopt_before_stop', (False, True))
def test_resource_completes_after_stop_or_after_adoption(adopt_before_stop: bool) -> None:
    before_stop: tuple[Event, ...] = (Acquired(SOCKET), Adopt(SOCKET)) if adopt_before_stop else ()
    supervisor = run(*spawned(), Acquire(SOCKET), *before_stop, Command.STOP, Command.CLOSE)
    assert_phase(supervisor, Phase.RETIRING)
    supervisor.accept(CleanupFinished(CHILD))
    assert_phase(supervisor, Phase.RETIRING)
    if not adopt_before_stop:
        supervisor.accept(Acquired(SOCKET))
        snapshot = supervisor.state
        assert supervisor.accept(Adopt(SOCKET)) == snapshot
    socket_cleanup = next(
        effect for effect in effects(supervisor.state, 'CLEAN') if effect.key == SOCKET
    )
    assert socket_cleanup.owner == (Owner.SESSION if adopt_before_stop else Owner.EFFECT)
    supervisor.accept(CleanupFinished(SOCKET))
    assert_phase(supervisor, Phase.CLOSED)
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'SESSION_CLOSED')


def test_acquisition_finishes_after_attempt_retirement_blocks_retry_until_disposed() -> None:
    supervisor = run(
        *spawned(),
        Acquire(SOCKET),
        ChildExited(1, 1, bind_collision=True),
        Command.CLOSE,
        CleanupFinished(CHILD),
        CommitRetry(1),
    )
    assert_phase(supervisor, Phase.RETIRING)
    assert supervisor.state.generation == 1
    supervisor.accept(Acquired(SOCKET))
    assert effects(supervisor.state, 'CLEAN')[-1] == Effect('CLEAN', SOCKET, Owner.EFFECT)
    supervisor.accept(CleanupFinished(SOCKET))
    supervisor.accept(CommitRetry(1))
    assert_phase(supervisor, Phase.STARTING)
    assert supervisor.state.generation == 2


def test_forward_batch_survives_interruption_before_rollback_and_close_failure() -> None:
    supervisor = run(
        *spawned(),
        Acquire(FORWARD_A),
        Acquired(FORWARD_A),
        Adopt(FORWARD_A),
        Acquire(FORWARD_B),
        Acquired(FORWARD_B),
        Adopt(FORWARD_B),
        StartupFailed(1, OPERATIONAL),
        Command.TERMINATION_SIGNAL,
    )
    assert_phase(supervisor, Phase.RETIRING)
    assert not effects(supervisor.state, 'CLEAN')
    assert all(resource.owner == Owner.SESSION for resource in supervisor.state.resources)
    supervisor.accept(Command.CLOSE)
    assert {effect.key for effect in effects(supervisor.state, 'CLEAN')} == {
        CHILD,
        FORWARD_A,
        FORWARD_B,
    }
    supervisor.accept(CleanupFinished(FORWARD_A, NESTED))
    assert_phase(supervisor, Phase.RETIRING)
    supervisor.accept(Command.TERMINATION_SIGNAL)
    supervisor.accept(Command.CLOSE)
    supervisor.accept(CleanupFinished(FORWARD_B))
    supervisor.accept(CleanupFinished(CHILD))
    assert_phase(supervisor, Phase.CLOSED)
    assert supervisor.state.outcome == model.Outcome(
        'failure', primary=OPERATIONAL, diagnostics=(NESTED,)
    )
    assert len(effects(supervisor.state, 'CLEAN')) == 3
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'ERROR')


@pytest.mark.parametrize('termination', (Command.CONTROL_EOF, Command.TERMINATION_SIGNAL))
def test_eof_and_signal_commit_without_inventing_wire_close_reason(termination: Command) -> None:
    supervisor = run(
        *spawned(),
        termination,
        ReadinessCandidate(1),
        CommitReady(1),
        Command.CLOSE,
        CleanupFinished(CHILD),
    )
    assert_phase(supervisor, Phase.CLOSED)
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING')


def test_spawn_failure_and_timeout_have_explicit_primary_outcomes() -> None:
    failure = run(
        Command.START, StartupFailed(1, OPERATIONAL, failed_acquisition=CHILD), Command.CLOSE
    )
    assert failure.state.phase == Phase.CLOSED
    assert failure.state.outcome == model.Outcome('failure', primary=OPERATIONAL)
    timed_out = run(*spawned(), StartupTimeout(1), Command.CLOSE, CleanupFinished(CHILD))
    assert timed_out.state.phase == Phase.CLOSED
    assert timed_out.state.outcome is not None
    assert timed_out.state.outcome.primary == Failure('TIMEOUT', 'startup timed out')
    assert (
        kinds(failure.state)
        == kinds(timed_out.state)
        == ('SESSION_CREATED', 'PROCESS_STARTING', 'ERROR')
    )


def test_late_failed_acquisition_settles_ticket_without_replacing_stop() -> None:
    supervisor = run(
        Command.START,
        Command.STOP,
        Command.CLOSE,
        StartupFailed(1, OPERATIONAL, failed_acquisition=CHILD),
    )
    assert_phase(supervisor, Phase.CLOSED)
    assert supervisor.state.outcome == model.Outcome('requested')


def test_cleanup_failure_prevents_retry_without_promoting_provisional_failure() -> None:
    supervisor = run(
        *spawned(),
        ChildExited(1, 1, bind_collision=True),
        Command.CLOSE,
        CleanupFinished(CHILD, NESTED),
        CommitRetry(1),
    )
    assert_phase(supervisor, Phase.CLOSED)
    assert supervisor.state.generation == 1
    assert supervisor.state.outcome is not None
    assert supervisor.state.outcome.primary == NESTED
    assert supervisor.state.attempt_failure == Failure('STARTUP_EXIT', 'child exited with 1')
    assert supervisor.state.outcome.diagnostics == (NESTED,)


def test_retry_exhaustion_is_terminal() -> None:
    child_two = ResourceKey(2, 'child')
    supervisor = run(
        *spawned(),
        ChildExited(1, 1, bind_collision=True),
        Command.CLOSE,
        CleanupFinished(CHILD),
        CommitRetry(1),
        Acquired(child_two),
        Adopt(child_two),
        ChildExited(2, 2, bind_collision=True),
        Command.CLOSE,
        CleanupFinished(child_two),
        CommitRetry(2),
    )
    assert_phase(supervisor, Phase.CLOSED)
    assert supervisor.state.generation == 2
    assert kinds(supervisor.state) == (
        'SESSION_CREATED',
        'PROCESS_STARTING',
        'PROCESS_STARTING',
        'ERROR',
    )


def test_natural_exit_preserves_result_through_stop() -> None:
    supervisor = run(
        *spawned(),
        ReadinessCandidate(1),
        CommitReady(1),
        ChildExited(1, 7),
        Command.STOP,
        Command.CLOSE,
        CleanupFinished(CHILD),
    )
    assert supervisor.state.outcome == model.Outcome('process_exit', 7)
    assert outputs(supervisor.state)[-1] == Output(
        'SESSION_CLOSED', outcome=model.Outcome('process_exit', 7)
    )


def test_control_batch_exposes_start_and_stop_at_one_commit() -> None:
    supervisor = run(ControlBatch((Command.START, Command.STOP)))
    assert_phase(supervisor, Phase.RETIRING)
    assert supervisor.state.outcome == model.Outcome('requested')
    supervisor.accept(Acquired(CHILD))
    pending = supervisor.state
    assert supervisor.accept(CommitReady(1)) == pending
    supervisor.accept(Command.CLOSE)
    supervisor.accept(CleanupFinished(CHILD))
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'SESSION_CLOSED')


def test_withheld_consumed_stop_requires_an_admission_fence() -> None:
    """Counterexample to an adapter that treats dispatch as observation."""
    consumed = ControlBatch((Command.STOP,))
    supervisor = run(*spawned(), ReadinessCandidate(1))
    supervisor.accept(CommitReady(1))
    assert_phase(supervisor, Phase.ACTIVE)
    supervisor.accept(consumed)
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING', 'PROCESS_READY')
    # No reducer can account for a batch hidden outside its admission boundary.
    admitted = run(*spawned(), ReadinessCandidate(1), consumed, CommitReady(1))
    assert admitted.state.phase == Phase.RETIRING
    assert kinds(admitted.state) == ('SESSION_CREATED', 'PROCESS_STARTING')


@pytest.mark.parametrize('retiring', (False, True), ids=('ready-boundary', 'retry-boundary'))
def test_admitted_control_failure_blocks_success_commits(retiring: bool) -> None:
    boundary: tuple[Event, ...]
    if retiring:
        boundary = (ChildExited(1, 1, bind_collision=True), Command.CLOSE, CleanupFinished(CHILD))
    else:
        boundary = (ReadinessCandidate(1),)
    control_failure = Failure('PROTOCOL', 'invalid control frame')
    supervisor = run(
        *spawned(), *boundary, ControlFailed(control_failure), CommitReady(1), CommitRetry(1)
    )
    assert_phase(supervisor, Phase.RETIRING)
    assert supervisor.state.outcome == model.Outcome('failure', primary=control_failure)
    assert kinds(supervisor.state) == ('SESSION_CREATED', 'PROCESS_STARTING')


def test_interruption_before_adoption_commit_leaves_effect_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = run(Command.START, Acquired(CHILD))
    before = supervisor.state

    def interrupt(state: State, event: Event) -> State:
        transition(state, event)  # Even a fully prepared adoption is uncommitted.
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(model, 'transition', interrupt)
        with pytest.raises(KeyboardInterrupt):
            supervisor.accept(Adopt(CHILD))
    assert supervisor.state == before
    assert supervisor.state.resources[0].owner == Owner.EFFECT
    supervisor.accept(Command.TERMINATION_SIGNAL)
    supervisor.accept(Command.CLOSE)
    assert effects(supervisor.state, 'CLEAN') == (Effect('CLEAN', CHILD, Owner.EFFECT),)


def test_interruption_after_commit_leaves_state_and_outbox_reviewable() -> None:
    supervisor = run(*spawned(), ReadinessCandidate(1))
    with pytest.raises(KeyboardInterrupt):
        supervisor.accept(CommitReady(1))
        raise KeyboardInterrupt
    assert_phase(supervisor, Phase.ACTIVE)
    assert outputs(supervisor.state)[-1] == Output('PROCESS_READY', 1)
    supervisor.accept(Command.TERMINATION_SIGNAL)
    supervisor.accept(Command.CLOSE)
    assert effects(supervisor.state, 'CLEAN') == (Effect('CLEAN', CHILD, Owner.SESSION),)


def test_bounded_arbitrary_sequences_preserve_invariants() -> None:
    """Explore six decisions from several boundaries; no timing or scheduling."""
    alphabet: tuple[Event, ...] = (
        Command.START,
        Command.STOP,
        Command.CONTROL_EOF,
        Command.TERMINATION_SIGNAL,
        Command.CLOSE,
        Acquired(CHILD),
        Adopt(CHILD),
        ReadinessCandidate(1),
        CommitReady(1),
        CommitRetry(1),
        ChildExited(1, 1, bind_collision=True),
        ChildExited(0, 99),
        StartupTimeout(1),
        StartupFailed(1, OPERATIONAL, failed_acquisition=CHILD),
        Acquire(FORWARD_A),
        Acquired(FORWARD_A),
        Adopt(FORWARD_A),
        CleanupFinished(CHILD),
        CleanupFinished(CHILD, NESTED),
        CleanupFinished(FORWARD_A),
    )
    seeds = (
        State(),
        run(*spawned()).state,
        run(*spawned(), ChildExited(1, 1, bind_collision=True), Command.CLOSE).state,
    )
    frontier = set(seeds)
    seen = {replace(state, outbox=()) for state in seeds}
    for _depth in range(6):
        following: set[State] = set()
        for state in frontier:
            for event in alphabet:
                after = transition(state, event)
                assert_invariants(state, after)
                # Outbox histories are checked on each edge; merge only decision-equivalent states.
                canonical = replace(after, outbox=())
                if canonical not in seen:
                    seen.add(canonical)
                    following.add(after)
        frontier = following
    assert any(state.phase == Phase.CLOSED for state in seen)
    assert any(state.generation == 2 for state in seen)
