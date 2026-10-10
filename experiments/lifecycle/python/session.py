# SPDX-License-Identifier: Apache-2.0
"""One loop-serialized decision authority with explicitly owned async effects."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .channels import Admission, ControlAdapter, SignalAdapter, Strategy, SuccessBarrier
from .contracts import (
    Acquisition,
    AttemptView,
    Bulk,
    ControlBatch,
    Diagnostic,
    EffectFailure,
    Effects,
    Fact,
    Finished,
    Frame,
    Kind,
    Outcome,
    Owner,
    Phase,
    ResourceOwner,
    Sink,
    Snapshot,
    Stage,
    diagnostic_from,
)
from .output import ProtocolWriter, frame


async def final_response[T](task: asyncio.Task[T]) -> T:
    """Cancellation of this wait cannot discard an independently running effect.

    There is deliberately no timeout-as-settlement fallback. Repeated caller
    cancellation still joins the owned final response; a nonresponding physical
    worker would require an adapter's real quiescence/reaping protocol.
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def dispose(scope: ResourceOwner) -> Diagnostic | None:
    assert scope.handle is not None
    try:
        await scope.handle.dispose()
    except BaseException as exception:
        return diagnostic_from(exception)
    return None


@dataclass
class Attempt:
    generation: int
    scope: ResourceOwner
    stage: Stage = Stage.AUTHORIZED
    worker: asyncio.Task[Finished] | None = None
    cleanup: asyncio.Task[Diagnostic | None] | None = None
    producer_settled: bool = False
    cleanup_accounted: bool = False
    observations: dict[Kind, asyncio.Future[Fact]] = field(default_factory=dict)
    accounted_observations: set[Kind] = field(default_factory=set)


async def execute(supervisor: Supervisor, attempt: Attempt) -> Finished:
    """Physical effect scope; only supervisor methods make session decisions."""
    production: asyncio.Task[None] | None = None
    failure: Diagnostic | None = None
    retryable = False
    interrupted = False
    cleanup: Diagnostic | None = None
    try:
        await supervisor.effects.before_entry(attempt.generation)
        operation = supervisor.execution_entry(attempt)
        if operation is None:
            return Finished()
        production = asyncio.create_task(operation.produce(attempt.scope), name='owned acquisition')
        await asyncio.shield(production)
        await operation.before_transfer()
        supervisor.adopt(attempt, operation)
    except asyncio.CancelledError:
        interrupted = True
    except BaseException as exception:
        failure = diagnostic_from(exception)
        retryable = isinstance(exception, EffectFailure) and exception.retryable
    finally:
        if production is not None:
            try:
                await final_response(production)
            except BaseException as exception:
                if failure is None:
                    failure = diagnostic_from(exception)
                    retryable = isinstance(exception, EffectFailure) and exception.retryable
        if attempt.scope.owner == Owner.PRODUCER and attempt.scope.handle is not None:
            cleanup = await final_response(asyncio.create_task(dispose(attempt.scope)))
    return Finished(failure, retryable, cleanup, interrupted)


class Supervisor:
    """Authority is loop affinity plus non-suspending decision methods.

    The coordinator continues accounting during barriers/output/worker waits.
    Executors rendezvous synchronously at actual entry and adoption on this
    same loop. An off-loop executor could not use these calls unchanged.
    """

    def __init__(
        self,
        effects: Effects,
        sink: Sink,
        *,
        strategy: Strategy = Strategy.ATOMIC,
        capacity: int = 1,
        output_capacity: int = 8,
        max_attempts: int = 2,
    ) -> None:
        self.effects = effects
        self.wake = asyncio.Event()
        self.changed = asyncio.Event()
        self.critical = Admission(capacity, self.wake)
        self.bulk: asyncio.Queue[Bulk] = asyncio.Queue(capacity)
        self.commands: asyncio.Queue[Attempt] = asyncio.Queue(capacity)
        self.control = ControlAdapter(strategy, self.critical)
        self.signals = SignalAdapter(self.wake)
        self.writer = ProtocolWriter(sink, self.wake, capacity=output_capacity)
        self.max_attempts = max_attempts
        self.phase = Phase.CREATED
        self.generation = 0
        self.returncode: int | None = None
        self.outcome = Outcome()
        self.candidate = False
        self.retry = False
        self.provisional: Diagnostic | None = None
        self.attempts: list[Attempt] = []
        self.protocol: list[Frame] = []  # observable experimental trace only
        self.barrier: SuccessBarrier | None = None
        self.epoch = 0
        self.terminal_selected = False
        self.terminal_pending: Frame | None = None
        self.terminal_committed = False
        self.writer_accounted = False
        self.control_accounted = False
        self.group: asyncio.TaskGroup | None = None
        self.control_task: asyncio.Task[Diagnostic | None] | None = None
        self.writer_task: asyncio.Task[None] | None = None

    def snapshot(self) -> Snapshot:
        return Snapshot(
            self.phase,
            self.generation,
            self.outcome,
            self.candidate,
            self.retry,
            self.barrier.kind if self.barrier is not None else None,
            tuple(
                AttemptView(
                    attempt.generation,
                    attempt.stage,
                    attempt.scope.owner,
                    attempt.scope.handle is not None,
                    attempt.scope.handle is not None and attempt.scope.handle.live,
                    attempt.producer_settled,
                )
                for attempt in self.attempts
            ),
            tuple(self.protocol),
            self.terminal_committed,
        )

    async def wait_for(self, predicate: Callable[[Snapshot], bool]) -> Snapshot:
        while not predicate(self.snapshot()):
            self.changed.clear()
            if predicate(self.snapshot()):
                break
            await self.changed.wait()
        return self.snapshot()

    async def observe(
        self,
        kind: Kind,
        generation: int = 0,
        failure: Diagnostic | None = None,
        *,
        returncode: int = 0,
    ) -> None:
        if self.phase != Phase.CLOSED:
            if kind in (Kind.EXIT, Kind.FAILURE) and generation:
                # Already recognized final child observations cannot await
                # mailbox credit. One original result slot per registered
                # one-shot source was reserved before the effect started.
                result = self.attempts[generation - 1].observations[kind]
                observation = Fact(kind, generation, failure, returncode)
                if result.done():
                    if result.result() != observation:
                        raise ValueError('one final fact per registered child source')
                else:
                    result.set_result(observation)
                self.wake.set()
                return
            await self.critical.publish(lambda: Fact(kind, generation, failure, returncode))

    def observe_nowait(self, kind: Kind, generation: int = 0) -> None:
        """Loop-local callback reserves credit before fact construction."""
        self.critical.publish_nowait(lambda: Fact(kind, generation))

    async def child_output(self, output: Bulk, *, ready_marker: bool = False) -> None:
        if ready_marker:
            await self.observe(Kind.READY, output.generation)
        await self.bulk.put(output)
        self.wake.set()

    def cancel_effect(self, generation: int) -> None:
        """A request; worker completion, not this call, proves settlement."""
        attempt = self.attempts[generation - 1]
        if attempt.worker is not None:
            attempt.worker.cancel()
        self.wake.set()

    def _abort_barrier(self) -> None:
        if self.barrier is not None:
            self.control.release(self.barrier)
            self.barrier = None

    def _terminate(self, reason: str) -> None:
        if self.outcome.reason is None:
            self.outcome = replace(self.outcome, reason=reason)
        self.phase = Phase.RETIRING
        self.candidate = False
        self.retry = False
        self._abort_barrier()
        self.control.stop()

    def _failure(self, failure: Diagnostic) -> None:
        if self.outcome.primary is None:
            self.outcome = replace(self.outcome, primary=failure)
        else:
            self.outcome = replace(self.outcome, diagnostics=(*self.outcome.diagnostics, failure))
        self._terminate('failure')

    def _retire(self, failure: Diagnostic) -> None:
        if self.outcome.reason is not None:
            return
        if self.generation >= self.max_attempts or self.phase != Phase.STARTING:
            self._failure(failure)
            return
        self.phase = Phase.RETIRING
        self.retry = True
        self.provisional = failure
        self.candidate = False
        self._abort_barrier()

    def _handle(self, observation: Fact | ControlBatch) -> None:
        if isinstance(observation, ControlBatch):
            for fact in observation.facts:
                self._handle(fact)
            return
        if observation.generation not in (0, self.generation):
            return
        kind = observation.kind
        if kind == Kind.START:
            if self.outcome.reason is None:
                if self.phase != Phase.CREATED:
                    self._failure(Diagnostic('protocol', 'duplicate START'))
                else:
                    self._new_attempt()
        elif kind in (Kind.STOP, Kind.EOF, Kind.SIGNAL):
            self._terminate(kind.value.lower())
        elif kind in (Kind.INVALID, Kind.FAILURE):
            self._failure(observation.failure or Diagnostic('operation', kind.value))
        elif self.outcome.reason is None:
            if kind == Kind.READY and self.phase == Phase.STARTING:
                self.candidate = True
            elif kind == Kind.RETRYABLE:
                self._retire(observation.failure or Diagnostic('bind', 'retryable startup failure'))
            elif kind == Kind.EXIT:
                if self.phase == Phase.ACTIVE:
                    self.returncode = observation.returncode
                    self._terminate('process-exited')
                else:
                    self._failure(Diagnostic('child-exit', 'child exited before readiness'))
            elif kind == Kind.TIMEOUT:
                self._failure(Diagnostic('startup-timeout', 'startup timed out'))

    def _account_ingress(self) -> None:
        if (signal := self.signals.take()) is not None:
            self._handle(signal)
        while not self.critical.queue.empty():
            observation = self.critical.take()
            try:
                self._handle(observation)
            finally:
                self.critical.queue.task_done()

    def _new_attempt(self) -> None:
        self.generation += 1
        self.phase = Phase.STARTING
        self.retry = False
        self.candidate = False
        self.provisional = None
        attempt = Attempt(self.generation, ResourceOwner(self.wake.set))
        loop = asyncio.get_running_loop()
        attempt.observations = {kind: loop.create_future() for kind in (Kind.EXIT, Kind.FAILURE)}
        self.attempts.append(attempt)

    def _commit_output(self, event: Frame) -> bool:
        assert not self.terminal_selected
        try:
            self.writer.admit(event)
        except EffectFailure as exception:
            self._failure(exception.diagnostic)
            return False
        self.protocol.append(event)
        return True

    def execution_entry(self, attempt: Attempt) -> Acquisition | None:
        with self.signals.commit_region():
            self._account_ingress()
            if (
                attempt.generation != self.generation
                or self.phase != Phase.STARTING
                or self.outcome.reason is not None
                or attempt.stage != Stage.QUEUED
            ):
                return None
            if not self._commit_output(
                frame('PROCESS_STARTING', attempt.generation, argv=['fake-openocd'])
            ):
                return None
            attempt.stage = Stage.RUNNING
            # This is actual entry, not permission for a later scheduling hop.
            operation = self.effects.begin(attempt.generation, attempt.scope)
        self.wake.set()
        return operation

    def adopt(self, attempt: Attempt, operation: Acquisition) -> bool:
        if (
            attempt.generation != self.generation
            or self.phase != Phase.STARTING
            or self.outcome.reason is not None
        ):
            return False
        # The cell was registered before execution. Even interruption after its
        # publication leaves a supervisor-reachable handle with one owner.
        operation.transfer(attempt.scope.adopt)
        attempt.stage = Stage.ADOPTED
        self.wake.set()
        return True

    async def _executor(self) -> None:
        assert self.group is not None
        while True:
            await self.effects.before_receive()
            attempt = await self.commands.get()
            try:
                if attempt.stage != Stage.SETTLED:
                    attempt.worker = self.group.create_task(execute(self, attempt))
                    attempt.worker.add_done_callback(lambda _task: self.wake.set())
            finally:
                self.commands.task_done()  # receipt, not physical-effect settlement

    def _account_results(self) -> None:
        if self.writer.failure is not None and not self.writer_accounted:
            self.writer_accounted = True
            self._failure(self.writer.failure)
        if (
            self.control_task is not None
            and self.control_task.done()
            and not self.control_accounted
        ):
            self.control_accounted = True
            original = self.control.recover_original()
            if original is not None:
                self._handle(original)
            if self.control_task.cancelled():
                if self.outcome.reason is None:
                    self._failure(Diagnostic('control-cancelled', 'control observer interrupted'))
            elif (exception := self.control_task.exception()) is not None:
                self._failure(diagnostic_from(exception))
            elif (failure := self.control_task.result()) is not None:
                self._failure(failure)
        for attempt in self.attempts:
            for kind, observation in attempt.observations.items():
                if observation.done() and kind not in attempt.accounted_observations:
                    attempt.accounted_observations.add(kind)
                    self._handle(observation.result())
            worker = attempt.worker
            if worker is not None and worker.done() and not attempt.producer_settled:
                result = Finished(interrupted=True) if worker.cancelled() else worker.result()
                attempt.producer_settled = True  # final task termination, not a timeout
                if result.failure is not None:
                    if result.retryable and self.outcome.reason is None:
                        self._retire(result.failure)
                    else:
                        self._failure(result.failure)
                if result.interrupted and self.outcome.reason is None and not self.retry:
                    self._failure(Diagnostic('effect-cancelled', 'startup effect interrupted'))
                if result.cleanup is not None:
                    if self.retry and self.provisional is not None:
                        self._failure(self.provisional)
                    self._failure(result.cleanup)
                if attempt.scope.owner == Owner.PRODUCER:
                    handle = attempt.scope.handle
                    attempt.scope.owner = Owner.SUPERVISOR if handle and handle.live else Owner.NONE
                    attempt.stage = Stage.SETTLED
            if (
                attempt.cleanup is not None
                and attempt.cleanup.done()
                and not attempt.cleanup_accounted
            ):
                attempt.cleanup_accounted = True
                failure = attempt.cleanup.result()
                if failure is not None:
                    if self.retry and self.provisional is not None:
                        self._failure(self.provisional)
                    self._failure(failure)
                if attempt.scope.handle is not None and not attempt.scope.handle.live:
                    attempt.scope.owner = Owner.NONE
                attempt.stage = Stage.SETTLED

    def _success_kind(self) -> str | None:
        if self.outcome.reason is not None or not self.attempts:
            return None
        attempt = self.attempts[-1]
        if self.phase == Phase.STARTING and self.candidate and attempt.stage == Stage.ADOPTED:
            return 'ready'
        if (
            self.phase == Phase.RETIRING
            and self.retry
            and attempt.stage == Stage.SETTLED
            and all(old.producer_settled and old.stage == Stage.SETTLED for old in self.attempts)
        ):
            return 'retry'
        return None

    def _success(self) -> None:
        kind = self._success_kind()
        if self.barrier is not None and (
            self.barrier.kind != kind or self.barrier.generation != self.generation
        ):
            self._abort_barrier()
        if kind is None:
            return
        if self.control.strategy == Strategy.PREFIX:
            if self.barrier is None:
                self.epoch += 1
                self.barrier = SuccessBarrier(self.epoch, kind, self.generation)
                self.control.request(self.barrier)
            if not self.barrier.acknowledged.is_set():
                return  # the coordinator keeps draining instead of awaiting ack
        with self.signals.commit_region():
            self._account_results()
            self._account_ingress()
            if self._success_kind() == kind:
                if kind == 'retry':
                    self._new_attempt()
                elif self._commit_output(
                    frame('PROCESS_READY', self.generation, remote_address='127.0.0.1', child_pid=1)
                ):
                    self.phase = Phase.ACTIVE
                    self.candidate = False
        self._abort_barrier()

    def _advance_effects(self) -> None:
        assert self.group is not None
        for attempt in self.attempts:
            if self.phase == Phase.RETIRING:
                if attempt.stage in (Stage.AUTHORIZED, Stage.QUEUED):
                    if attempt.worker is None:
                        attempt.producer_settled = True
                        attempt.scope.owner = Owner.NONE
                        attempt.stage = Stage.SETTLED
                    else:
                        attempt.worker.cancel()
                if (
                    attempt.scope.owner == Owner.SUPERVISOR
                    and attempt.scope.handle is not None
                    and attempt.stage != Stage.SETTLED
                    and attempt.cleanup is None
                ):
                    attempt.stage = Stage.CLEANING
                    attempt.cleanup = self.group.create_task(dispose(attempt.scope))
                    attempt.cleanup.add_done_callback(lambda _task: self.wake.set())
            elif attempt.stage == Stage.AUTHORIZED and not self.commands.full():
                self.commands.put_nowait(attempt)
                attempt.stage = Stage.QUEUED

    def _terminal_output(self) -> None:
        if self.outcome.reason is None:
            return
        if not self.control_accounted or any(
            not attempt.producer_settled or attempt.stage != Stage.SETTLED
            for attempt in self.attempts
        ):
            return
        if not self.critical.queue.empty() or not self.bulk.empty():
            return
        if not self.terminal_selected:
            self.terminal_selected = True
            if self.outcome.primary is not None:
                message = '; '.join(
                    (
                        self.outcome.primary.render(),
                        *(diagnostic.render() for diagnostic in self.outcome.diagnostics),
                    )
                )
                self.terminal_pending = frame('ERROR', message=message, code='EXPERIMENT')
            elif self.outcome.reason in ('stop', 'process-exited'):
                reason = 'requested' if self.outcome.reason == 'stop' else 'process_exit'
                self.terminal_pending = frame(
                    'SESSION_CLOSED', reason=reason, returncode=self.returncode
                )
            if self.terminal_pending is not None:
                self.protocol.append(self.terminal_pending)  # reserve final descriptor
                self.terminal_committed = True
        if self.terminal_pending is not None:
            if self.writer.failure is not None:
                self.terminal_pending = None
            else:
                try:
                    self.writer.admit(self.terminal_pending)
                except EffectFailure as exception:
                    if exception.diagnostic.code != 'output-oversize':
                        return  # pending terminal retains its own bounded credit
                    self._failure(exception.diagnostic)
                self.terminal_pending = None
        self.writer.finish()
        if self.writer_task is not None and self.writer_task.done():
            self.phase = Phase.CLOSED

    def _turn(self) -> None:
        self._account_results()
        self._account_ingress()
        while not self.bulk.empty():
            fragment = self.bulk.get_nowait()
            if (
                fragment.generation == self.generation
                and not self.terminal_selected
                and self.writer.failure is None
            ):
                self._commit_output(
                    frame(
                        'CHILD_OUTPUT',
                        fragment.generation,
                        payload=fragment.payload,
                        stream=fragment.stream,
                        line_end=True,
                    )
                )
        self._advance_effects()
        self._success()
        # A retry may have authorized a new proposal in this same turn.
        self._advance_effects()
        self._terminal_output()
        self.changed.set()

    async def _coordinate(self) -> None:
        while self.phase != Phase.CLOSED:
            self.wake.clear()
            self._turn()
            if self.phase != Phase.CLOSED:
                await self.wake.wait()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            self.group = group
            self.writer_task = group.create_task(self.writer.run())
            self.writer_task.add_done_callback(lambda _task: self.wake.set())
            self.control_task = group.create_task(self.control.run())
            executor = group.create_task(self._executor())
            self._commit_output(
                frame(
                    'SESSION_CREATED',
                    helper='experimental',
                    session_id='fake',
                    remote_workspace='experimental',
                )
            )
            try:
                while self.phase != Phase.CLOSED:
                    try:
                        await self._coordinate()
                    except asyncio.CancelledError:
                        self._terminate('cancelled')
            finally:
                executor.cancel()
                self.group = None
