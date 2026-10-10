# SPDX-License-Identifier: Apache-2.0
"""Controller-lease authority over actual Popen, raw pipes and POSIX signals."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace

from .model import (
    Active,
    Authorized,
    ChildResult,
    Cleaned,
    Closed,
    ControllerEnded,
    Created,
    Deadline,
    Diagnostic,
    Exited,
    Fact,
    Failure,
    Marker,
    Offered,
    Outcome,
    Owned,
    Producing,
    Request,
    Settled,
    Signal,
    Start,
    Starting,
    State,
    Terminating,
    attempt_of,
    failure_record,
    weakened,
)
from .streams import Stream
from .unix import ByteWriter, ProcessScope, RealTimers, SignalCapture, Ticket, Timers
from .workspace import Workspace


class Runtime:
    def __init__(
        self,
        control_fd: int,
        output_fd: int,
        workspace: Workspace,
        *,
        timers: Timers | None = None,
        pass_fds: tuple[int, ...] = (),
        handoff: Callable[[Ticket], Awaitable[None]] | None = None,
        interrupt: Callable[[str, Ticket], None] | None = None,
    ) -> None:
        self.control_fd = control_fd
        self.workspace = workspace
        self.timers = timers or RealTimers()
        self.pass_fds = pass_fds
        self.handoff = handoff
        self.interrupt = interrupt or (lambda _point, _ticket: None)
        self.state: State = Created()
        self.tickets: dict[int, Ticket] = {}
        self.streams: dict[tuple[int, str], Stream] = {}
        self.facts: deque[Fact] = deque()
        self.changed = asyncio.Event()
        self.observed = asyncio.Condition()
        self.writer = ByteWriter(output_fd)
        self.writer.on_failure = lambda failure: self.admit(Failure(failure))
        self.writer.on_progress = self.changed.set
        self.signals = SignalCapture(lambda signum: self.admit(Signal(signum)))
        self.control_buffer = bytearray()
        self.control_started = False
        self.deadline: asyncio.Future[None] | None = None
        self.cleanup_workspace: asyncio.Task[tuple[Diagnostic, ...]] | None = None
        self.committed: dict[str, int] = {
            kind: 0 for kind in ('ATTEMPT', 'READY', 'CHILD_OUTPUT', 'SESSION_ENDED')
        }
        self.final_observations = 0

    @property
    def terminal_snapshot(self) -> Outcome | None:
        return self.state.snapshot if isinstance(self.state, Closed) else None

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        async with self.observed:
            await self.observed.wait_for(predicate)

    def admit(self, fact: Fact) -> None:
        # Source cardinality is bounded: START/EOF once, two streams, <=16
        # marker names, <=8 attempts and finite final results. Sources retain
        # original physical ownership; this never waits on bulk/output capacity.
        if len(self.facts) >= 128:
            raise RuntimeError('critical source bound exceeded')
        self.facts.append(fact)
        self.changed.set()

    def observe_control(self) -> None:
        try:
            chunk = os.read(self.control_fd, 4096)
        except BlockingIOError:
            return
        if not chunk:
            asyncio.get_running_loop().remove_reader(self.control_fd)
            if self.control_buffer:
                self.admit(Failure(Diagnostic('control', 'incomplete START')))
            self.admit(ControllerEnded())
            return
        self.control_buffer.extend(chunk)
        if len(self.control_buffer) > 16384:
            self.control_buffer.clear()
            self.admit(Failure(Diagnostic('control', 'input limit')))
            asyncio.get_running_loop().remove_reader(self.control_fd)
            return
        if self.control_started:
            self.admit(Failure(Diagnostic('control', 'lease permits no post-START commands')))
            self.control_buffer.clear()
            asyncio.get_running_loop().remove_reader(self.control_fd)
            return
        if b'\n' not in self.control_buffer:
            return
        line, rest = self.control_buffer.split(b'\n', 1)
        self.control_buffer = bytearray(rest)
        try:
            value = json.loads(line)
            if (
                set(value) != {'type', 'argv', 'required', 'policy', 'max_attempts'}
                or value['type'] != 'START'
            ):
                raise ValueError('invalid START fields')
            argv, required = value['argv'], value['required']
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(arg, str) for arg in argv)
            ):
                raise ValueError('invalid argv')
            if (
                not isinstance(required, list)
                or len(required) > 16
                or not all(
                    isinstance(marker, str) and marker and len(marker) <= 256 for marker in required
                )
            ):
                raise ValueError('invalid readiness markers')
            if value['policy'] not in ('live', 'exit') or not 1 <= value['max_attempts'] <= 8:
                raise ValueError('invalid policy')
            request = Request(
                tuple(argv),
                frozenset(required),
                value['policy'],
                value['max_attempts'],
                self.pass_fds,
            )
            self.control_started = True
            self.admit(Start(request))
            if rest:
                self.admit(Failure(Diagnostic('control', 'unexpected input after START')))
        except (ValueError, TypeError) as error:
            self.control_buffer.clear()
            self.admit(Failure(Diagnostic('control', str(error))))
            asyncio.get_running_loop().remove_reader(self.control_fd)

    def publish(self, kind: str, **payload: object) -> bool:
        if isinstance(self.state, Closed):
            return False
        if not self.writer.admit({'type': kind, **payload}):
            return False
        if kind in self.committed:
            self.committed[kind] += 1
        return True

    def terminate(self, trigger: str, failure: Diagnostic | None = None) -> None:
        self.changed.set()
        if isinstance(self.state, Closed):
            if failure:
                self.state = replace(self.state, outcome=self.state.outcome.fail(failure))
            return
        if isinstance(self.state, Terminating):
            outcome = self.state.outcome
        else:
            history = self.state.history if isinstance(self.state, (Starting, Active)) else ()
            if (
                isinstance(self.state, Starting)
                and self.state.provisional is not None
                and self.state.provisional != failure
            ):
                history = (*history, self.state.provisional)
            outcome = Outcome(trigger, diagnostics=history)
        if failure:
            outcome = outcome.fail(failure)
        attempt = attempt_of(self.state)
        if isinstance(attempt, Authorized):
            attempt = Settled(attempt.generation)
        self.state = Terminating(attempt, outcome)
        if self.deadline is not None:
            self.deadline.cancel()

    def enter(self, request: Request, generation: int) -> None:
        eligible = (
            isinstance(self.state, Starting)
            and isinstance(self.state.attempt, Authorized)
            and self.state.attempt.generation == generation
        )
        if not eligible and not weakened('attempt-after-terminal'):
            return
        if not self.publish('ATTEMPT', generation=generation, argv=list(request.argv)):
            self.terminate('failure', Diagnostic('writer', 'ATTEMPT admission failed'))
            return
        ticket = Ticket(generation)
        self.tickets[generation] = ticket
        if eligible:
            assert isinstance(self.state, Starting)
            self.state = replace(self.state, attempt=Producing(ticket))
            if request.policy == 'live' and self.deadline is None:
                self.deadline = self.timers.arm('startup', 30)
                self.deadline.add_done_callback(
                    lambda future: self.admit(Deadline(generation))
                    if not future.cancelled()
                    else None
                )
        try:
            # Native handlers only latch; coroutine cancellation cannot enter
            # this synchronous physical boundary. Ticket owns the result before
            # descriptor setup or adoption can raise.
            ticket.process = subprocess.Popen(
                request.argv,
                cwd=self.workspace.path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=request.pass_fds,
                env={**os.environ, 'ZRO_LEASE_GENERATION': str(generation)},
            )
            self.interrupt('returned', ticket)
            ticket.scope = ProcessScope(ticket.process)
            if weakened('lose-ownership'):
                self.tickets.pop(generation)
            self.interrupt('acquired', ticket)

            def report_stream_failure(failure: Diagnostic) -> None:
                self.admit(Failure(failure, generation))

            for name in ('stdout', 'stderr'):
                stream = getattr(ticket.process, name)
                assert stream is not None
                self.streams[generation, name] = Stream(
                    stream.fileno(),
                    name,
                    generation,
                    request.required,
                    self.admit,
                    self.changed.set,
                    report_stream_failure,
                )
            asyncio.get_running_loop().add_reader(ticket.scope.pidfd, self.observe_exit, ticket)
            if self.handoff is None:
                self.adopt(ticket)
            else:
                ticket.producer = asyncio.create_task(self.finish_producer(ticket))
            self.changed.set()
        except BaseException as error:
            # Synchronous adoption hooks can fail immediately before/after
            # publication. The registered ticket still owns the Popen object.
            if ticket.process is None:
                ticket.settled = True
            self.terminate('failure', failure_record('acquire', error))

    async def finish_producer(self, ticket: Ticket) -> None:
        assert self.handoff is not None
        try:
            await self.handoff(ticket)
        except BaseException as error:
            self.admit(Failure(failure_record('producer', error)))
        finally:
            self.admit(Offered(ticket))

    def adopt(self, ticket: Ticket) -> None:
        if ticket.settled and not weakened('stale-adopt'):
            return
        current = attempt_of(self.state)
        if isinstance(current, Owned) and current.ticket is ticket:
            return
        if (
            isinstance(self.state, Starting)
            and current is not None
            and (current.generation == ticket.generation or weakened('stale-adopt'))
        ):
            self.interrupt('before-adopt', ticket)
            self.state = replace(self.state, attempt=Owned(ticket))
            self.interrupt('after-adopt', ticket)
            if self.state.request.policy == 'live' and self.deadline is None:
                self.deadline = self.timers.arm('startup', 30)
                generation = ticket.generation
                self.deadline.add_done_callback(
                    lambda future: self.admit(Deadline(generation))
                    if not future.cancelled()
                    else None
                )
        # Otherwise the producer's original ticket permits late disposal,
        # without ownership transfer into current lifecycle state.

    def observe_exit(self, ticket: Ticket) -> None:
        assert ticket.scope is not None
        returncode = ticket.scope.poll()
        if returncode is not None:
            asyncio.get_running_loop().remove_reader(ticket.scope.pidfd)
            self.admit(Exited(ticket.generation, returncode))

    def final_scan(self, generation: int) -> None:
        self.final_observations += 1
        for (gen, _), stream in self.streams.items():
            if gen == generation:
                stream.final_scan()
        ticket = self.tickets.get(generation)
        if ticket is not None and ticket.scope is not None and not ticket.settled:
            self.observe_exit(ticket)

    def account(self, fact: Fact) -> None:
        if isinstance(fact, Start):
            if isinstance(self.state, Created):
                self.state = Starting(fact.request, Authorized(1))
            else:
                self.terminate('failure', Diagnostic('control', 'START repeated'))
        elif isinstance(fact, ControllerEnded):
            self.terminate('controller-ended')
        elif isinstance(fact, Failure):
            attempt = attempt_of(self.state)
            if fact.generation is None or (
                attempt is not None and attempt.generation == fact.generation
            ):
                self.terminate('failure', fact.diagnostic)
        elif isinstance(fact, Signal):
            if not isinstance(self.state, (Terminating, Closed)):
                self.terminate(f'signal-{fact.signum}')
        elif isinstance(fact, Marker):
            if isinstance(self.state, Starting) and (
                fact.generation == self.state.attempt.generation or weakened('stale-adopt')
            ):
                self.state = replace(self.state, evidence=self.state.evidence | {fact.name})
        elif isinstance(fact, Offered):
            # Task completion, not cancellation request, is the settlement seam.
            if fact.ticket.producer is not None and not fact.ticket.producer.done():
                raise RuntimeError('producer result before genuine final response')
            self.adopt(fact.ticket)
        elif isinstance(fact, Exited):
            attempt = attempt_of(self.state)
            if attempt is None or attempt.generation != fact.generation:
                return
            if isinstance(self.state, Starting):
                # Drain finite startup prefix before classifying a bind failure.
                for (gen, _), stream in self.streams.items():
                    if gen == fact.generation:
                        stream.final_scan()
                collision = any(
                    stream.collision
                    for (gen, _), stream in self.streams.items()
                    if gen == fact.generation
                )
                if collision and fact.generation < self.state.request.max_attempts:
                    self.state = replace(
                        self.state,
                        provisional=Diagnostic(
                            'startup',
                            'bind collision',
                            child=ChildResult(fact.generation, fact.returncode),
                        ),
                    )
                    return
                failure = None
                if self.state.request.policy == 'live':
                    failure = Diagnostic('openocd', 'exit before readiness')
                elif fact.returncode:
                    failure = Diagnostic('openocd', f'exit {fact.returncode}')
                self.terminate('process-exit', failure)
            elif isinstance(self.state, Active):
                self.terminate(
                    'process-exit',
                    Diagnostic('openocd', f'exit {fact.returncode}') if fact.returncode else None,
                )
            if isinstance(self.state, Terminating):
                self.state = replace(
                    self.state,
                    outcome=replace(
                        self.state.outcome, child=ChildResult(fact.generation, fact.returncode)
                    ),
                )
        elif isinstance(fact, Cleaned):
            fact.ticket.settled = True
            if isinstance(self.state, Terminating):
                outcome = self.state.outcome
                for failure in fact.failures:
                    outcome = outcome.fail(failure)
                self.state = replace(self.state, outcome=outcome)
            elif (
                isinstance(self.state, Starting)
                and self.state.attempt.generation == fact.ticket.generation
            ):
                if fact.failures:
                    self.terminate('failure', self.state.provisional)
                    for failure in fact.failures:
                        self.terminate('failure', failure)
                else:
                    self.state = replace(self.state, attempt=Settled(fact.ticket.generation))
        elif isinstance(fact, Deadline) and (
            isinstance(self.state, Starting) and self.state.attempt.generation == fact.generation
        ):
            self.final_scan(fact.generation)
            while self.facts:
                self.account(self.facts.popleft())
            if (
                isinstance(self.state, Starting)
                and not self.state.request.required <= self.state.evidence
            ):
                self.terminate('failure', Diagnostic('startup', 'readiness timeout'))

    async def clean_ticket(self, ticket: Ticket) -> Cleaned:
        failures: tuple[Diagnostic, ...] = ()
        if ticket.producer is not None:
            await ticket.producer  # Final response, not cancellation request, permits disposal.
        if ticket.scope is not None:
            asyncio.get_running_loop().remove_reader(ticket.scope.pidfd)
            # Genuine exit already observable before cleanup is retained; a
            # forced cleanup wait result is not labelled a natural child exit.
            try:
                result = ticket.scope.poll()
                if result is not None:
                    self.admit(Exited(ticket.generation, result))
            except Exception as error:
                failures = (Diagnostic('cleanup', str(error)),)
            failures = (*failures, *await ticket.scope.cleanup(self.timers, ticket.generation))
            for (gen, _), stream in self.streams.items():
                if gen == ticket.generation:
                    try:
                        stream.final_scan()
                    except OSError as error:
                        failures = (*failures, Diagnostic('cleanup', str(error)))
                    finally:
                        stream.close()
            failures = (*failures, *ticket.scope.close_descriptors())
        elif ticket.process is not None:
            # Descriptor/adoption setup failed after Popen returned; root ticket
            # retains the process even if Scope construction failed.
            try:
                os.killpg(ticket.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as error:
                failures = (*failures, Diagnostic('cleanup', str(error)))
            try:
                ticket.process.wait(timeout=5)
            except Exception as error:
                failures = (*failures, Diagnostic('cleanup', str(error)))
            for pipe in (ticket.process.stdout, ticket.process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError as error:
                        failures = (*failures, Diagnostic('cleanup', str(error)))
            try:
                os.killpg(ticket.process.pid, 0)
            except ProcessLookupError:
                pass
            except OSError as error:
                failures = (*failures, failure_record('cleanup', error))
            else:
                failures = (*failures, Diagnostic('cleanup', 'group disappearance unconfirmed'))
        disposed = ticket.scope.closed if ticket.scope is not None else not failures
        cleaned = Cleaned(ticket, failures, disposed)
        self.admit(cleaned)
        return cleaned

    def drive(self) -> None:
        if isinstance(self.state, Starting):
            if isinstance(self.state.attempt, Authorized):
                self.enter(self.state.request, self.state.attempt.generation)
            elif self.state.provisional is not None:
                attempt = self.state.attempt
                if isinstance(attempt, Settled) or weakened('early-retry'):
                    if self.deadline is not None:
                        self.deadline.cancel()
                    self.deadline = None
                    self.state = Starting(
                        self.state.request,
                        Authorized(attempt.generation + 1),
                        history=(*self.state.history, self.state.provisional),
                    )
                    self.changed.set()
                elif isinstance(attempt, Owned):
                    self.schedule_cleanup(attempt.ticket)
            elif (
                isinstance(self.state.attempt, Owned)
                and self.state.request.policy == 'live'
                and self.state.request.required <= self.state.evidence
            ):
                scope = self.state.attempt.ticket.scope
                assert scope is not None
                result = scope.poll()
                if result is not None:
                    self.account(Exited(self.state.attempt.generation, result))
                    self.changed.set()
                    return
                if self.publish(
                    'READY',
                    generation=self.state.attempt.generation,
                    pid=self.state.attempt.ticket.process.pid
                    if self.state.attempt.ticket.process
                    else None,
                ):
                    self.state = Active(self.state.request, self.state.attempt, self.state.history)
                    if self.deadline is not None:
                        self.deadline.cancel()
                else:
                    self.terminate('failure', Diagnostic('writer', 'READY admission failed'))
        if isinstance(self.state, Terminating):
            for ticket in self.tickets.values():
                self.schedule_cleanup(ticket)
            if self.cleanup_workspace is None:
                predecessors = tuple(
                    ticket.cleaning
                    for ticket in self.tickets.values()
                    if ticket.cleaning is not None
                )
                self.cleanup_workspace = asyncio.create_task(
                    self.workspace.cleanup(self.timers, predecessors)
                )
                self.cleanup_workspace.add_done_callback(lambda _: self.changed.set())

    def schedule_cleanup(self, ticket: Ticket) -> None:
        if not ticket.settled and ticket.cleaning is None:
            ticket.cleaning = asyncio.create_task(self.clean_ticket(ticket))

    def relay(self) -> None:
        for stream in self.streams.values():
            while stream.pending:
                if not self.publish(
                    'CHILD_OUTPUT',
                    generation=stream.generation,
                    stream=stream.name,
                    text=stream.pending[0],
                ):
                    break
                stream.pop()
            stream.resume()

    async def finish_bulk(self) -> asyncio.Future[None]:
        """Resource cleanup already settled; give retained output one finite budget."""
        budget = self.timers.arm('output-drain', 10)
        async with self.observed:
            self.observed.notify_all()
        while any(stream.pending for stream in self.streams.values()):
            while self.facts:
                self.account(self.facts.popleft())
            self.relay()
            if not any(stream.pending for stream in self.streams.values()) or self.writer.failure:
                break
            if any(
                len(
                    self.writer.encode(
                        {
                            'type': 'CHILD_OUTPUT',
                            'generation': stream.generation,
                            'stream': stream.name,
                            'text': stream.pending[0],
                        }
                    )
                )
                > self.writer.capacity
                for stream in self.streams.values()
                if stream.pending
            ):
                self.terminate('failure', Diagnostic('writer', 'output frame exceeds capacity'))
                break
            self.changed.clear()
            response = asyncio.create_task(self.changed.wait())
            try:
                done, _ = await asyncio.wait(
                    (response, budget), return_when=asyncio.FIRST_COMPLETED
                )
                if budget in done:
                    self.terminate('failure', Diagnostic('writer', 'final output drain deadline'))
                    break
            except asyncio.CancelledError:
                self.terminate('failure', Diagnostic('writer', 'final bulk drain cancelled'))
                break
            finally:
                response.cancel()
                await asyncio.gather(response, return_exceptions=True)
        return budget

    async def run(self) -> Closed:
        self.signals.install()
        os.set_blocking(self.control_fd, False)
        asyncio.get_running_loop().add_reader(self.control_fd, self.observe_control)
        self.publish('SESSION_CREATED', workspace=str(self.workspace.path), helper_pid=os.getpid())
        final_budget: asyncio.Future[None] | None = None
        try:
            while not isinstance(self.state, Closed):
                try:
                    await self.changed.wait()
                except asyncio.CancelledError:
                    # Cancellation requests termination; it does not settle any
                    # producer or abandon owned physical cleanup.
                    self.terminate('cancelled', Diagnostic('authority', 'task cancelled'))
                    self.changed.set()
                self.changed.clear()
                while self.facts:
                    self.account(self.facts.popleft())
                self.relay()
                self.drive()
                if (
                    isinstance(self.state, Terminating)
                    and all(ticket.settled for ticket in self.tickets.values())
                    and self.cleanup_workspace is not None
                    and self.cleanup_workspace.done()
                ):
                    final_budget = await self.finish_bulk()
                    assert isinstance(self.state, Terminating)
                    outcome = self.state.outcome
                    for failure in self.cleanup_workspace.result():
                        outcome = outcome.fail(failure)
                    # Bulk retained before termination must precede terminal.
                    self.relay()
                    if any(stream.pending for stream in self.streams.values()):
                        outcome = outcome.fail(
                            Diagnostic('writer', 'undelivered child output at closure')
                        )
                    residuals = tuple(
                        f'process-{ticket.process.pid}'
                        for ticket in self.tickets.values()
                        if ticket.process is not None
                        and (
                            ticket.cleaning is None
                            or not ticket.cleaning.done()
                            or not ticket.cleaning.result().disposed
                        )
                    )
                    if self.workspace.path.exists():
                        residuals = (*residuals, 'workspace')
                    self.state = Closed(outcome, residuals, outcome)
                    self.committed['SESSION_ENDED'] += 1
                    async with self.observed:
                        self.observed.notify_all()
                    try:
                        admitted = await self.writer.admit_terminal(
                            {
                                'type': 'SESSION_ENDED',
                                **asdict(outcome),
                                'disposal_confirmed': not residuals
                                and not self.workspace.path.exists(),
                            },
                            final_budget,
                        )
                    except asyncio.CancelledError:
                        admitted = False
                        if not final_budget.done():
                            final_budget.set_result(None)
                    if not admitted:
                        outcome = outcome.fail(Diagnostic('writer', 'terminal admission failed'))
                        self.state = replace(self.state, outcome=outcome)
                async with self.observed:
                    self.observed.notify_all()
            try:
                drain_failure = await self.writer.drain(self.timers, final_budget)
            except asyncio.CancelledError:
                drain_failure = Diagnostic('writer', 'final drain cancelled')
            while self.facts:
                self.account(self.facts.popleft())
            if drain_failure is not None:
                if (
                    drain_failure != self.state.outcome.primary
                    and drain_failure not in self.state.outcome.diagnostics
                ):
                    self.terminate('failure', drain_failure)
                if weakened('second-terminal'):
                    self.committed['SESSION_ENDED'] += 1
                    self.writer.admit({'type': 'SESSION_ENDED', 'trigger': 'writer-failure'})
            assert isinstance(self.state, Closed)
            return self.state
        finally:
            asyncio.get_running_loop().remove_reader(self.control_fd)
            for ticket in self.tickets.values():
                if ticket.scope is not None and not ticket.scope.descriptors_closed:
                    asyncio.get_running_loop().remove_reader(ticket.scope.pidfd)
            self.signals.close()
            self.writer.close()
