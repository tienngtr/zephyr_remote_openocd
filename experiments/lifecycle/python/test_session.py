# SPDX-License-Identifier: Apache-2.0
"""Real asyncio interleavings at controlled physical boundaries; no sleeps."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from zephyr_remote_openocd.remote.protocol import EventOrder, decode_single_frame

from .channels import Admission, ControlAdapter, Strategy
from .contracts import (
    Bulk,
    Diagnostic,
    EffectFailure,
    Fact,
    Kind,
    Owner,
    Phase,
    Snapshot,
    Stage,
    diagnostic_from,
)
from .fakes import ControlledAcquisition, ControlledEffects, ControlledSink, Gate
from .session import Supervisor

OPERATIONAL = Diagnostic('operation', 'startup failed')
WRITER = Diagnostic('writer', 'write failed')
CLEANUP = Diagnostic('cleanup', 'cleanup failed', (Diagnostic('nested', 'close detail'),))


def run(coroutine: Coroutine[None, None, None]) -> None:
    async def bounded() -> None:
        # Deadlock safety net only; no successful history depends on expiry.
        async with asyncio.timeout(30):
            await coroutine

    asyncio.run(bounded())


def kinds(snapshot: Snapshot) -> tuple[str, ...]:
    return tuple(event.kind for event in snapshot.protocol)


def invariants(supervisor: Supervisor) -> None:
    state = supervisor.snapshot()
    terminal = [
        index
        for index, event in enumerate(state.protocol)
        if event.kind in ('ERROR', 'SESSION_CLOSED')
    ]
    assert len(terminal) <= 1
    if terminal:
        assert terminal == [len(state.protocol) - 1]
    for attempt in state.attempts:
        if attempt.live:
            assert attempt.owner != Owner.NONE
        if attempt.generation < state.generation:
            assert attempt.producer_settled and attempt.stage == Stage.SETTLED
    # These event snapshots satisfy corrected production's actual wire validator.
    order = EventOrder()
    for event in state.protocol:
        order.accept(decode_single_frame(event.encoded))


@dataclass
class Experiment:
    supervisor: Supervisor
    effects: ControlledEffects
    sink: ControlledSink
    task: asyncio.Task[None]

    async def start(self) -> None:
        self.supervisor.control.accept_read(b'START\n')
        await self.effects.plans[0].entered.wait()

    async def adopted(self, generation: int = 1) -> None:
        self.effects.plans[generation - 1].completion_gate.release()
        await self.supervisor.wait_for(
            lambda state: len(state.attempts) >= generation
            and state.attempts[generation - 1].stage == Stage.ADOPTED
        )

    async def close(self) -> Snapshot:
        await self.task
        invariants(self.supervisor)
        return self.supervisor.snapshot()


@asynccontextmanager
async def experiment(
    *plans: ControlledAcquisition,
    strategy: Strategy = Strategy.ATOMIC,
    output_capacity: int = 8,
    automatic_writer: bool = True,
    capacity: int = 1,
    pause_receive: bool = False,
) -> AsyncIterator[Experiment]:
    effects = ControlledEffects(
        *(plans or (ControlledAcquisition(), ControlledAcquisition())), pause_receive=pause_receive
    )
    sink = ControlledSink(automatic=automatic_writer)
    supervisor = Supervisor(
        effects, sink, strategy=strategy, output_capacity=output_capacity, capacity=capacity
    )
    task = asyncio.create_task(supervisor.run())
    fixture = Experiment(supervisor, effects, sink, task)
    try:
        await supervisor.wait_for(lambda state: 'SESSION_CREATED' in kinds(state))
        if automatic_writer:
            await sink.wait_for(lambda output: bytes(output.wire).endswith(b'\n'))
        else:
            await sink.wait_for(lambda output: bool(output.requests))
        yield fixture
    finally:
        if not task.done():
            effects.receive_gate.release()
            for plan in effects.plans:
                plan.release_all()
            sink.unblock()
            await supervisor.observe(Kind.STOP)
        await task
        invariants(supervisor)
        for plan in effects.plans:
            if plan.handle is not None:
                assert len(plan.handle.cleaned_by) <= 1
                if plan.handle.live:
                    assert plan.handle.scope.owner == Owner.SUPERVISOR


@pytest.mark.parametrize('strategy', tuple(Strategy))
def test_stop_admitted_after_candidate_prevents_ready(strategy: Strategy) -> None:
    async def scenario() -> None:
        async with experiment(strategy=strategy, capacity=2) as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.READY, 1)
            # Two available credits make both admissions non-suspending. The
            # authority accounts STOP before attempting the candidate commit.
            await fixture.supervisor.observe(Kind.STOP)
            state = await fixture.close()
            assert 'PROCESS_READY' not in kinds(state)

    run(scenario())


def test_withheld_stop_blocks_ready_and_barrier_keeps_draining() -> None:
    async def scenario() -> None:
        async with experiment(strategy=Strategy.PREFIX) as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.READY, 1)
            fixture.supervisor.control.accept_read(b'STOP\n')
            assert fixture.supervisor.critical.queue.full()
            assert fixture.supervisor.control.original is not None
            assert fixture.supervisor.control.recognized > fixture.supervisor.control.admitted
            state = await fixture.close()
            assert 'PROCESS_READY' not in kinds(state)
            assert fixture.supervisor.epoch > 0
            assert fixture.supervisor.control.recognized == fixture.supervisor.control.admitted
            assert fixture.supervisor.control.barrier is None

    run(scenario())


@pytest.mark.parametrize('strategy', tuple(Strategy))
def test_start_stop_same_consumed_batch_has_no_spawn(strategy: Strategy) -> None:
    async def scenario() -> None:
        async with experiment(strategy=strategy) as fixture:
            fixture.supervisor.control.accept_read(b'START\nSTOP\n')
            state = await fixture.close()
            assert fixture.effects.started == []
            assert kinds(state) == ('SESSION_CREATED', 'SESSION_CLOSED')

    run(scenario())


@pytest.mark.parametrize('strategy', tuple(Strategy))
def test_ready_then_stop_retains_ready(strategy: Strategy) -> None:
    async def scenario() -> None:
        async with experiment(strategy=strategy) as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.READY, 1)
            await fixture.supervisor.wait_for(lambda state: state.phase == Phase.ACTIVE)
            fixture.supervisor.control.accept_read(b'STOP\n')
            state = await fixture.close()
            assert kinds(state) == (
                'SESSION_CREATED',
                'PROCESS_STARTING',
                'PROCESS_READY',
                'SESSION_CLOSED',
            )
            assert fixture.supervisor.writer.delivered == list(state.protocol)

    run(scenario())


def test_recognized_stop_at_cleanup_final_response_prevents_retry() -> None:
    async def scenario() -> None:
        async with experiment(strategy=Strategy.PREFIX) as fixture:
            await fixture.start()
            await fixture.adopted()
            handle = fixture.effects.plans[0].handle
            assert handle is not None

            def final_cleanup_observations() -> None:
                # Cleanup has physically completed. Its final response and this
                # separately recognized STOP precede retry decision accounting.
                fixture.supervisor.observe_nowait(Kind.READY, 1)
                fixture.supervisor.control.accept_read(b'STOP\n')
                assert fixture.supervisor.critical.queue.full()
                assert fixture.supervisor.control.original is not None

            handle.on_cleanup = final_cleanup_observations
            await fixture.supervisor.observe(Kind.RETRYABLE, 1)
            state = await fixture.close()
            assert state.generation == 1
            assert fixture.effects.started == [1]
            assert state.outcome.primary is None
            assert 'PROCESS_READY' not in kinds(state)

    run(scenario())


@pytest.mark.parametrize('strategy', tuple(Strategy))
def test_cancelled_wait_does_not_settle_late_producer_or_allow_retry(strategy: Strategy) -> None:
    async def scenario() -> None:
        async with experiment(strategy=strategy) as fixture:
            await fixture.start()
            await fixture.supervisor.observe(Kind.RETRYABLE, 1)
            await fixture.supervisor.wait_for(lambda state: state.retry)
            fixture.supervisor.cancel_effect(1)
            # Inject a later harmless observation as a public accounting
            # handshake; expiry is never used to infer forbidden progress.
            await fixture.supervisor.observe(Kind.READY, 1)
            await fixture.supervisor.critical.queue.join()
            assert fixture.supervisor.snapshot().generation == 1
            assert not fixture.supervisor.snapshot().attempts[0].producer_settled
            fixture.effects.plans[0].completion_gate.release()
            await fixture.effects.plans[1].entered.wait()
            state = fixture.supervisor.snapshot()
            assert state.generation == 2 and state.attempts[0].producer_settled
            handle = fixture.effects.plans[0].handle
            assert handle is not None and handle.cleaned_by == [Owner.PRODUCER]

    run(scenario())


@pytest.mark.parametrize('strategy', tuple(Strategy))
def test_retry_and_stale_observations_are_fenced(strategy: Strategy) -> None:
    async def scenario() -> None:
        first = ControlledAcquisition(failure=OPERATIONAL, retryable=True)
        async with experiment(first, ControlledAcquisition(), strategy=strategy) as fixture:
            await fixture.start()
            first.completion_gate.release()
            await fixture.effects.plans[1].entered.wait()
            await fixture.adopted(2)
            await fixture.supervisor.observe(Kind.READY, 1)
            await fixture.supervisor.observe(Kind.EXIT, 1)
            await fixture.supervisor.observe(Kind.FAILURE, 1, OPERATIONAL)
            await fixture.supervisor.observe(Kind.READY, 2)
            state = await fixture.supervisor.wait_for(lambda state: state.phase == Phase.ACTIVE)
            assert state.generation == 2 and state.outcome.primary is None
            assert [
                event.generation for event in state.protocol if event.kind == 'PROCESS_READY'
            ] == [2]

    run(scenario())


@pytest.mark.parametrize('ignore_cancellation', (False, True))
def test_queued_same_generation_spawn_is_revoked(ignore_cancellation: bool) -> None:
    async def scenario() -> None:
        plan = ControlledAcquisition(
            pause_entry=True, ignore_entry_cancellation=ignore_cancellation
        )
        async with experiment(plan) as fixture:
            fixture.supervisor.control.accept_read(b'START\n')
            await fixture.supervisor.wait_for(
                lambda state: bool(state.attempts) and state.attempts[0].stage == Stage.QUEUED
            )
            await plan.at_entry.wait()
            await fixture.supervisor.observe(Kind.STOP)
            await fixture.supervisor.wait_for(lambda state: state.outcome.reason == 'stop')
            plan.entry_gate.release()
            if ignore_cancellation:
                await plan.entry_returned.wait()
                assert fixture.effects.started == []
            state = await fixture.close()
            assert fixture.effects.started == []
            assert 'PROCESS_STARTING' not in kinds(state)

    run(scenario())


def test_running_effect_returning_after_stop_is_producer_disposed() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.supervisor.observe(Kind.STOP)
            await fixture.supervisor.wait_for(lambda state: state.outcome.reason == 'stop')
            assert not fixture.supervisor.snapshot().attempts[0].producer_settled
            fixture.effects.plans[0].completion_gate.release()
            state = await fixture.close()
            handle = fixture.effects.plans[0].handle
            assert handle is not None and handle.cleaned_by == [Owner.PRODUCER]
            assert state.attempts[0].owner == Owner.NONE

    run(scenario())


@pytest.mark.parametrize('after_adoption', (False, True))
def test_termination_around_adoption_has_one_cleanup_owner(after_adoption: bool) -> None:
    async def scenario() -> None:
        plan = ControlledAcquisition(pause_transfer=not after_adoption)
        async with experiment(plan) as fixture:
            await fixture.start()
            plan.completion_gate.release()
            await plan.at_transfer.wait()
            if after_adoption:
                await fixture.supervisor.wait_for(
                    lambda state: state.attempts[0].owner == Owner.SUPERVISOR
                )
            else:
                assert fixture.supervisor.snapshot().attempts[0].owner == Owner.PRODUCER
            await fixture.supervisor.observe(Kind.STOP)
            await fixture.supervisor.wait_for(lambda state: state.outcome.reason == 'stop')
            plan.transfer_gate.release()
            await fixture.close()
            assert plan.handle is not None
            assert plan.handle.cleaned_by == [
                Owner.SUPERVISOR if after_adoption else Owner.PRODUCER
            ]

    run(scenario())


@pytest.mark.parametrize('point', ('before', 'after'))
def test_interruption_inside_transfer_uses_published_owner(point: str) -> None:
    async def scenario() -> None:
        plan = ControlledAcquisition(transfer_interrupt=point)
        async with experiment(plan) as fixture:
            await fixture.start()
            plan.completion_gate.release()
            state = await fixture.close()
            assert state.outcome.primary is not None
            assert plan.handle is not None
            assert plan.handle.cleaned_by == [
                Owner.PRODUCER if point == 'before' else Owner.SUPERVISOR
            ]

    run(scenario())


def test_real_task_cancellation_before_adoption_recovers_owned_resource() -> None:
    async def scenario() -> None:
        plan = ControlledAcquisition(pause_transfer=True)
        async with experiment(plan) as fixture:
            await fixture.start()
            plan.completion_gate.release()
            await plan.at_transfer.wait()
            fixture.supervisor.cancel_effect(1)
            await fixture.close()
            assert plan.handle is not None and plan.handle.cleaned_by == [Owner.PRODUCER]

    run(scenario())


def test_process_starting_full_buffer_prevents_effect_entry() -> None:
    async def scenario() -> None:
        async with experiment(output_capacity=1, automatic_writer=False) as fixture:
            fixture.supervisor.control.accept_read(b'START\n')
            state = await fixture.supervisor.wait_for(
                lambda state: state.outcome.primary is not None
            )
            assert fixture.effects.started == []
            assert 'PROCESS_STARTING' not in kinds(state)
            fixture.sink.fail(WRITER)
            await fixture.close()

    run(scenario())


def test_writer_failure_after_starting_does_not_undo_owned_effect() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            fixture.sink.automatic = False
            await fixture.start()
            await fixture.sink.wait_for(lambda output: len(output.requests) >= 2)
            fixture.sink.fail(WRITER)
            await fixture.supervisor.wait_for(lambda state: state.outcome.primary == WRITER)
            fixture.effects.plans[0].completion_gate.release()
            state = await fixture.close()
            assert fixture.effects.started == [1]
            assert 'PROCESS_STARTING' in kinds(state)
            assert state.outcome.primary == WRITER
            handle = fixture.effects.plans[0].handle
            assert handle is not None and handle.cleaned_by == [Owner.PRODUCER]

    run(scenario())


def test_ready_full_output_buffer_prevents_ready_commit() -> None:
    async def scenario() -> None:
        async with experiment(output_capacity=1) as fixture:
            fixture.sink.automatic = False
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.READY, 1)
            state = await fixture.supervisor.wait_for(
                lambda state: state.outcome.primary is not None
            )
            assert 'PROCESS_READY' not in kinds(state)
            fixture.sink.fail(WRITER)
            await fixture.close()

    run(scenario())


@pytest.mark.parametrize('partial', (False, True))
def test_terminal_writer_failure_retains_primary_nested_cleanup_and_no_replay(
    partial: bool,
) -> None:
    async def scenario() -> None:
        plan = ControlledAcquisition(cleanup_failure=CLEANUP)
        async with experiment(plan) as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.sink.wait_for(lambda output: bytes(output.wire).count(b'\n') == 2)
            fixture.sink.automatic = False
            await fixture.supervisor.observe(Kind.FAILURE, 1, OPERATIONAL)
            await fixture.supervisor.wait_for(lambda state: state.terminal_committed)
            await fixture.sink.wait_for(lambda output: len(output.requests) >= 3)
            if partial:
                fixture.sink.advance(3)
                await fixture.sink.wait_for(lambda output: len(output.requests) >= 4)
                assert fixture.supervisor.writer.offset == 3
            fixture.sink.fail(WRITER)
            state = await fixture.close()
            assert state.outcome.primary == OPERATIONAL
            assert state.outcome.diagnostics == (CLEANUP, WRITER)
            assert state.outcome.diagnostics[0].details == CLEANUP.details
            assert kinds(state).count('ERROR') == 1
            assert bytes(fixture.sink.wire).count(b'\n') == 2
            assert state.attempts[0].owner == Owner.SUPERVISOR and state.attempts[0].live

    run(scenario())


def test_terminal_pending_behind_full_buffer_can_fail_locally() -> None:
    async def scenario() -> None:
        async with experiment(output_capacity=1) as fixture:
            fixture.sink.automatic = False
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.FAILURE, 1, OPERATIONAL)
            state = await fixture.supervisor.wait_for(lambda state: state.terminal_committed)
            assert kinds(state)[-1] == 'ERROR'
            assert fixture.supervisor.writer.current is not None
            assert fixture.supervisor.writer.current.kind == 'PROCESS_STARTING'
            fixture.sink.fail(WRITER)
            state = await fixture.close()
            assert state.outcome.primary == OPERATIONAL
            assert state.outcome.diagnostics == (WRITER,)
            assert kinds(state).count('ERROR') == 1

    run(scenario())


@pytest.mark.parametrize('strategy', tuple(Strategy))
@pytest.mark.parametrize('tail', (b'STOP\n', None, b'INVALID\n'))
def test_control_tail_terminates_pending_startup(strategy: Strategy, tail: bytes | None) -> None:
    async def scenario() -> None:
        async with experiment(strategy=strategy) as fixture:
            await fixture.start()
            fixture.supervisor.control.accept_read(tail)
            await fixture.supervisor.wait_for(lambda state: state.outcome.reason is not None)
            fixture.effects.plans[0].completion_gate.release()
            state = await fixture.close()
            assert 'PROCESS_READY' not in kinds(state)
            if tail is None:
                assert kinds(state) == ('SESSION_CREATED', 'PROCESS_STARTING')

    run(scenario())


def test_latched_signal_defeats_ready_without_queue_publication() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.READY, 1)
            # This is the native-handler seam: no Queue/Event mutation.
            fixture.supervisor.signals.latch.capture(15)
            state = await fixture.close()
            assert state.outcome.reason == 'signal'
            assert 'PROCESS_READY' not in kinds(state)

    run(scenario())


def test_readiness_marker_admission_precedes_blocked_bulk() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.child_output(Bulk(1, 'first'))
            producer = asyncio.create_task(
                fixture.supervisor.child_output(Bulk(1, 'READY'), ready_marker=True)
            )
            await fixture.supervisor.wait_for(lambda state: state.phase == Phase.ACTIVE)
            await producer
            assert 'PROCESS_READY' in kinds(fixture.supervisor.snapshot())

    run(scenario())


def test_outer_cancellation_joins_late_producer_and_writer() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            fixture.task.cancel()
            await fixture.supervisor.wait_for(lambda state: state.outcome.reason == 'cancelled')
            assert not fixture.task.done()
            fixture.task.cancel()  # a second request still cannot abandon the owned producer
            fixture.effects.plans[0].completion_gate.release()
            state = await fixture.close()
            assert state.attempts[0].producer_settled
            assert (
                fixture.supervisor.writer_task is not None and fixture.supervisor.writer_task.done()
            )

    run(scenario())


def test_cleanup_can_start_before_cancelled_producer_final_response() -> None:
    async def scenario() -> None:
        plan = ControlledAcquisition(pause_transfer=True)
        async with experiment(plan) as fixture:
            await fixture.start()
            plan.completion_gate.release()
            await plan.at_transfer.wait()
            assert plan.handle is not None
            plan.handle.cleanup_gate = Gate()
            fixture.supervisor.cancel_effect(1)
            await plan.handle.cleaning.wait()
            assert not fixture.supervisor.snapshot().attempts[0].producer_settled
            plan.handle.cleanup_gate.release()
            await fixture.close()

    run(scenario())


def test_prefix_admission_publishes_the_original_batch_object() -> None:
    async def scenario() -> None:
        wake = asyncio.Event()
        admission = Admission(1, wake)
        source = ControlAdapter(Strategy.PREFIX, admission)
        source.accept_read(b'START\nSTOP\n')
        original = source.original
        source.stop()
        publisher = asyncio.create_task(source.run())
        await wake.wait()
        assert admission.take() is original
        await publisher
        assert source.original is None

    run(scenario())


def test_cancelled_control_publication_recovers_its_original_stop() -> None:
    async def scenario() -> None:
        async with experiment(strategy=Strategy.PREFIX) as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.READY, 1)
            fixture.supervisor.control.accept_read(b'STOP\n')
            assert fixture.supervisor.control.original is not None
            assert fixture.supervisor.control_task is not None
            fixture.supervisor.control_task.cancel()
            state = await fixture.close()
            assert state.outcome.reason == 'stop'
            assert 'PROCESS_READY' not in kinds(state)
            assert fixture.supervisor.control.original is None

    run(scenario())


def test_old_resource_adoption_and_duplicate_result_cannot_affect_retry() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.RETRYABLE, 1)
            await fixture.effects.plans[1].entered.wait()
            old = fixture.supervisor.attempts[0]
            assert old.producer_settled and old.scope.owner == Owner.NONE
            assert not fixture.supervisor.adopt(old, fixture.effects.plans[0])
            assert old.worker is not None and old.worker.done()
            previous = old.worker.result()  # retained immutable result, already accounted
            await fixture.supervisor.observe(Kind.EXIT, 1)
            await fixture.supervisor.critical.queue.join()
            assert old.worker.result() is previous
            assert fixture.supervisor.snapshot().generation == 2
            assert fixture.supervisor.snapshot().outcome.primary is None

    run(scenario())


@pytest.mark.parametrize('partial', (False, True))
def test_closed_frame_failure_is_only_a_local_delivery_failure(partial: bool) -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.sink.wait_for(lambda output: bytes(output.wire).count(b'\n') == 2)
            fixture.sink.automatic = False
            await fixture.supervisor.observe(Kind.STOP)
            await fixture.supervisor.wait_for(lambda state: state.terminal_committed)
            await fixture.sink.wait_for(lambda output: len(output.requests) >= 3)
            if partial:
                fixture.sink.advance(2)
                await fixture.sink.wait_for(lambda output: len(output.requests) >= 4)
            fixture.sink.fail(WRITER)
            state = await fixture.close()
            assert state.outcome.reason == 'stop' and state.outcome.primary == WRITER
            assert kinds(state)[-1] == 'SESSION_CLOSED'
            assert 'ERROR' not in kinds(state)

    run(scenario())


def test_startup_timeout_does_not_prove_producer_settlement() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.supervisor.observe(Kind.TIMEOUT, 1)
            await fixture.supervisor.wait_for(lambda state: state.outcome.reason == 'failure')
            state = fixture.supervisor.snapshot()
            assert not state.attempts[0].producer_settled and state.generation == 1
            fixture.effects.plans[0].completion_gate.release()
            state = await fixture.close()
            assert state.generation == 1 and fixture.effects.started == [1]

    run(scenario())


def test_child_exit_result_is_accounted_despite_full_critical_mailbox() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.READY, 1)
            assert fixture.supervisor.critical.queue.full()
            await fixture.supervisor.observe(Kind.EXIT, 1)
            state = await fixture.close()
            assert state.outcome.primary is not None
            assert state.outcome.primary.code == 'child-exit'
            assert 'PROCESS_READY' not in kinds(state)

    run(scenario())


def test_writer_task_cancellation_is_retained_as_a_local_failure() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            fixture.sink.automatic = False
            await fixture.start()
            await fixture.sink.wait_for(lambda output: len(output.requests) >= 2)
            assert fixture.supervisor.writer_task is not None
            fixture.supervisor.writer_task.cancel()
            await fixture.supervisor.wait_for(lambda state: state.outcome.primary is not None)
            fixture.effects.plans[0].completion_gate.release()
            state = await fixture.close()
            assert state.outcome.primary is not None
            assert state.outcome.primary.code == 'CancelledError'

    run(scenario())


def test_natural_child_exit_preserves_protocol_result_fields() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.adopted()
            await fixture.supervisor.observe(Kind.READY, 1)
            await fixture.supervisor.wait_for(lambda state: state.phase == Phase.ACTIVE)
            await fixture.supervisor.observe(Kind.EXIT, 1, returncode=7)
            state = await fixture.close()
            assert decode_single_frame(state.protocol[-1].encoded)['reason'] == 'process_exit'
            assert decode_single_frame(state.protocol[-1].encoded)['returncode'] == 7

    run(scenario())


@pytest.mark.parametrize('interrupted', (False, True))
def test_begin_acquisition_then_exception_before_return_retains_cleanup_scope(
    interrupted: bool,
) -> None:
    async def scenario() -> None:
        failure = asyncio.CancelledError() if interrupted else EffectFailure(OPERATIONAL)
        plan = ControlledAcquisition(begin_failure=failure)
        async with experiment(plan) as fixture:
            fixture.supervisor.control.accept_read(b'START\n')
            state = await fixture.close()
            assert plan.handle is not None and plan.handle.cleaned_by == [Owner.PRODUCER]
            assert state.outcome.primary is not None
            assert 'PROCESS_STARTING' in kinds(state)

    run(scenario())


def test_oversize_terminal_decision_finishes_with_local_diagnostic() -> None:
    async def scenario() -> None:
        async with experiment() as fixture:
            await fixture.start()
            await fixture.adopted()
            failure = Diagnostic('operation', 'x' * 5000)
            await fixture.supervisor.observe(Kind.FAILURE, 1, failure)
            state = await fixture.close()
            assert state.outcome.primary == failure
            assert state.outcome.diagnostics[-1].code == 'output-oversize'
            assert kinds(state).count('ERROR') == 1
            assert all(event.kind != 'ERROR' for event in fixture.supervisor.writer.delivered)

    run(scenario())


def test_revoked_command_dequeued_after_stop_cannot_start() -> None:
    async def scenario() -> None:
        async with experiment(pause_receive=True) as fixture:
            fixture.sink.automatic = False  # hold final delivery while executor resumes
            fixture.supervisor.control.accept_read(b'START\n')
            await fixture.supervisor.wait_for(
                lambda state: bool(state.attempts) and state.attempts[0].stage == Stage.QUEUED
            )
            assert fixture.supervisor.commands.full()
            await fixture.supervisor.observe(Kind.STOP)
            await fixture.supervisor.wait_for(lambda state: state.outcome.reason == 'stop')
            fixture.effects.receive_gate.release()
            await fixture.supervisor.commands.join()
            assert fixture.effects.started == []
            assert 'PROCESS_STARTING' not in kinds(fixture.supervisor.snapshot())
            fixture.sink.unblock()
            await fixture.close()

    run(scenario())


def test_atomic_credit_wait_does_not_consume_eof_and_can_stop_cleanly() -> None:
    async def scenario() -> None:
        admission = Admission(1, asyncio.Event())
        admission.publish_nowait(lambda: Fact(Kind.READY))
        source = ControlAdapter(Strategy.ATOMIC, admission)
        source.accept_read(None)
        publisher = asyncio.create_task(source.run())
        await admission.backpressured.wait()
        assert source.recognized == 0 and len(source.reads) == 1
        source.stop()
        admission.take()  # real consumer releases the bounded channel's credit
        assert await publisher is None
        assert source.recognized == 0 and source.finished

    run(scenario())


def test_physical_exception_conversion_snapshots_nested_group_and_notes() -> None:
    leaf = OSError('stream close failed')
    leaf.add_note('nested close detail')
    failure = ExceptionGroup('cleanup failed', [leaf, ValueError('group close failed')])
    failure.add_note('outer cleanup detail')
    result = diagnostic_from(failure)
    leaf.add_note('later mutation')
    assert result.details[0].message == 'stream close failed'
    assert result.details[0].details == (Diagnostic('exception-note', 'nested close detail'),)
    assert result.details[1].message == 'group close failed'
    assert result.details[2] == Diagnostic('exception-note', 'outer cleanup detail')
