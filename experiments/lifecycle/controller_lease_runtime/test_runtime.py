# SPDX-License-Identifier: Apache-2.0
"""Real process/pipe tests of product outcomes and cleanup ownership."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from .harness import Harness, write
from .model import (
    Active,
    ChildResult,
    Diagnostic,
    Failure,
    Offered,
    Owned,
    Producing,
    Request,
    Starting,
    Terminating,
    attempt_of,
)
from .unix import Ticket, readable


@pytest.mark.parametrize('profile,status', (('flash-ok', 0), ('flash-fail', 7)))
def test_flash_result_and_attempt_diagnostic(tmp_path: Path, profile: str, status: int) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start(profile=profile, policy='exit', required=())
            assert run.task is not None
            closed = await run.task
            assert closed.outcome.child == ChildResult(1, status)
            assert (closed.outcome.primary is not None) == bool(status)
            assert run.runtime.committed['READY'] == 0
            attempt = await run.frame('ATTEMPT')
            assert attempt['argv'] == list(run.argv)
            assert not closed.residuals and not run.workspace.path.exists()

    asyncio.run(scenario())


def test_real_lease_eof_preserves_final_stdout_and_per_stream_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            await run.emit('stdout', b'prefix \xe2')
            await run.emit('stdout', b'\x82\xac no newline')
            async with run.frames_changed:
                await run.frames_changed.wait_for(
                    lambda: '\u20ac no newline'
                    in ''.join(str(frame.get('text', '')) for frame in run.frames)
                )
            await run.emit('stdout', b'\n')
            await run.ready()
            closed = await run.finish()
            assert closed.outcome.trigger == 'controller-ended'
            final = await run.frame('SESSION_ENDED')
            assert final['primary'] is None
            text = ''.join(
                str(frame['text'])
                for frame in run.frames
                if frame['type'] == 'CHILD_OUTPUT' and frame['stream'] == 'stdout'
            )
            assert 'prefix \u20ac no newline\nINIT\n' in text
            assert run.runtime.committed['SESSION_ENDED'] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ('markers', 'exit', 'stream-eof', 'controller-eof', 'empty'))
def test_real_deadline_final_observations(tmp_path: Path, boundary: str) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            await run.runtime.wait_for(
                lambda: isinstance(run.runtime.state, Starting)
                and isinstance(run.runtime.state.attempt, Owned)
            )
            # Real ordinary-reader backpressure seam: final scan bypasses pause,
            # owns the same fd, and reads actual kernel bytes/EOF. No fake markers.
            for stream in run.runtime.streams.values():
                stream.pause()
            if boundary == 'markers':
                await run.emit('stdout', b' INIT \n')
                await run.emit('stderr', b'STARTUP\n')
            elif boundary == 'exit':
                ticket = run.current_ticket()
                assert ticket.scope is not None
                asyncio.get_running_loop().remove_reader(ticket.scope.pidfd)
                await run.command('exit', returncode=9)
                await readable(ticket.scope.pidfd)
            elif boundary == 'stream-eof':
                await run.command('close-output')
            elif boundary == 'controller-eof':
                run.close_lease()
            run.timers.expire('startup')
            if boundary == 'markers':
                await run.frame('READY')
                assert isinstance(run.runtime.state, Active)
                assert run.runtime.final_observations >= 1
                await run.finish()
            else:
                assert run.task is not None
                closed = await run.task
                assert run.runtime.committed['READY'] == 0
                if boundary == 'exit':
                    assert closed.outcome.child == ChildResult(1, 9)
                elif boundary == 'controller-eof':
                    assert closed.outcome.trigger in ('controller-ended', 'failure')
                else:
                    assert closed.outcome.primary is not None

    asyncio.run(scenario())


@pytest.mark.parametrize('signum', (signal.SIGINT, signal.SIGTERM))
def test_real_native_signal_cleanup(tmp_path: Path, signum: int) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            os.kill(os.getpid(), signum)
            assert run.task is not None
            closed = await run.task
            assert closed.outcome.trigger == f'signal-{signum}'
            assert not closed.residuals
            assert all(
                ticket.process is not None and ticket.process.returncode is not None
                for ticket in run.runtime.tickets.values()
            )

    asyncio.run(scenario())


@pytest.mark.parametrize('point', ('returned', 'acquired', 'before-adopt', 'after-adopt'))
def test_real_acquisition_interruption_keeps_cleanup_owner(tmp_path: Path, point: str) -> None:
    class BoundaryInterruption(BaseException):
        pass

    captured: list[Ticket] = []

    def interrupt(where: str, ticket: Ticket) -> None:
        captured.append(ticket)
        if where == point:
            raise BoundaryInterruption()

    async def scenario() -> None:
        async with Harness(tmp_path, interrupt=interrupt) as run:
            # Hook may interrupt before stand-in emits its receipt. START pipe
            # remains real; synchronize on the returned terminal, not that receipt.
            argv = (
                sys.executable,
                '-u',
                str(Path(__file__).with_name('standin.py')),
                '--profile',
                'stall',
            )

            await write(
                run.control_write,
                (
                    json.dumps(
                        {
                            'type': 'START',
                            'argv': argv,
                            'required': [],
                            'policy': 'live',
                            'max_attempts': 1,
                        }
                    )
                    + '\n'
                ).encode(),
            )
            assert run.task is not None
            try:
                closed = await run.task
                assert closed.outcome.primary is not None
                assert captured and captured[0].process is not None
                assert captured[0].process.returncode is not None, (
                    'acquired process escaped cleanup'
                )
                assert not closed.residuals
            finally:
                run.saved.extend(captured)

    asyncio.run(scenario())


def test_real_signal_inside_synchronous_handoff_is_latched(tmp_path: Path) -> None:
    def interrupt(where: str, _ticket: Ticket) -> None:
        if where == 'before-adopt':
            os.kill(os.getpid(), signal.SIGTERM)

    async def scenario() -> None:
        async with Harness(tmp_path, interrupt=interrupt) as run:
            await run.send_start()
            assert run.task is not None
            closed = await run.task
            assert closed.outcome.trigger == f'signal-{signal.SIGTERM}'
            assert not closed.residuals

    asyncio.run(scenario())


def test_retry_requires_true_producer_settlement(tmp_path: Path) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def handoff(_ticket: Ticket) -> None:
            entered.set()
            await release.wait()

        async with Harness(tmp_path, handoff=handoff) as run:
            try:
                await run.start(profile='bind')
                await entered.wait()
                original = run.runtime.tickets[1]
                await run.runtime.wait_for(
                    lambda: original.scope is not None and original.scope.poll() is not None
                )
                attempt = attempt_of(run.runtime.state)
                assert attempt is not None and attempt.generation == 1, (
                    'retry entered before producer settlement'
                )
                assert not original.settled
                assert isinstance(attempt_of(run.runtime.state), Producing)
                release.set()
                await run.receipt_kind('spawned', generation=2)
                assert run.runtime.tickets[1].settled
                await run.runtime.wait_for(
                    lambda: isinstance(run.runtime.state, Starting)
                    and isinstance(run.runtime.state.attempt, Owned)
                    and run.runtime.state.attempt.generation == 2
                )
                await run.ready()
                closed = await run.finish()
                assert any(
                    value.child == ChildResult(1, 12) for value in closed.outcome.diagnostics
                )
                assert closed.outcome.child is None
            finally:
                release.set()

    asyncio.run(scenario())


def test_timeout_is_not_pending_producer_settlement(tmp_path: Path) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def handoff(_ticket: Ticket) -> None:
            entered.set()
            await release.wait()

        async with Harness(tmp_path, handoff=handoff) as run:
            try:
                await run.start()
                await entered.wait()
                run.timers.expire('startup')
                await run.runtime.wait_for(lambda: isinstance(run.runtime.state, Terminating))
                assert not run.runtime.tickets[1].settled
                assert run.runtime.committed['SESSION_ENDED'] == 0
                assert run.workspace.path.exists()
                release.set()
                assert run.task is not None
                closed = await run.task
                assert closed.outcome.primary is not None
                assert not closed.residuals
            finally:
                release.set()

    asyncio.run(scenario())


def test_stale_offer_cannot_replace_current_attempt(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start(profile='bind')
            await run.receipt_kind('spawned', generation=2)
            await run.runtime.wait_for(
                lambda: isinstance(run.runtime.state, Starting)
                and isinstance(run.runtime.state.attempt, Owned)
                and run.runtime.state.attempt.generation == 2
            )
            old = run.runtime.tickets[1]
            run.runtime.account(Offered(old))
            current = attempt_of(run.runtime.state)
            assert current is not None and current.generation == 2
            assert old.settled
            await run.finish()

    asyncio.run(scenario())


def test_terminal_authority_forbids_attempt_entry(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            run.runtime.terminate('controller-ended')
            request = Request(
                (
                    sys.executable,
                    '-u',
                    str(Path(__file__).with_name('standin.py')),
                    '--profile',
                    'stall',
                ),
                frozenset(),
            )
            run.runtime.enter(request, 1)
            assert run.runtime.committed['ATTEMPT'] == 0
            assert not run.runtime.tickets
            await run.finish()

    asyncio.run(scenario())


def test_group_cleanup_reaps_descendant_after_leader_exit(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            await run.ready()
            await run.command('fork')
            child = await run.receipt_kind('descendant')
            pid = child['pid']
            assert isinstance(pid, int)
            await run.command('exit', returncode=0)
            assert run.task is not None
            closed = await run.task
            assert closed.outcome.child == ChildResult(1, 0)
            assert not closed.residuals
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)

    asyncio.run(scenario())


def test_sigkill_escalation_uses_real_ignored_sigterm(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            await run.command('ignore-term')
            run.close_lease()
            await run.receipt_kind('signal')
            await run.timers.wait_armed('term-1')
            assert run.workspace.path.exists(), 'child still owns its staged working inputs'
            run.timers.expire('term-1')
            assert run.task is not None
            closed = await run.task
            scope = run.runtime.tickets[1].scope
            assert scope is not None and scope.escalated and scope.closed
            assert not closed.residuals

    asyncio.run(scenario())


def test_primary_is_preserved_when_real_workspace_cleanup_fails(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            primary = Diagnostic(
                'operation', 'established', (Diagnostic('nested', 'original detail'),)
            )
            with run.workspace.staging():
                run.runtime.admit(Failure(primary))
                assert run.task is not None
                closed = await run.task
                assert closed.outcome.primary == primary
                assert any(failure.source == 'cleanup' for failure in closed.outcome.diagnostics)
                assert run.workspace.path.exists()
                assert (
                    run.runtime.tickets[1].scope is not None and run.runtime.tickets[1].scope.closed
                )

    asyncio.run(scenario())


def test_controller_eof_before_late_producer_response(tmp_path: Path) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def handoff(_ticket: Ticket) -> None:
            await release.wait()

        async with Harness(tmp_path, handoff=handoff) as run:
            try:
                await run.start()
                run.close_lease()
                await run.runtime.wait_for(lambda: isinstance(run.runtime.state, Terminating))
                ticket = run.runtime.tickets[1]
                assert not ticket.settled
                assert run.runtime.committed['SESSION_ENDED'] == 0
                release.set()
                assert run.task is not None
                closed = await run.task
                assert run.runtime.committed['READY'] == 0
                assert ticket.scope is not None and ticket.scope.closed
                assert not closed.residuals
            finally:
                release.set()

    asyncio.run(scenario())


def test_accounted_eof_defeats_retry_without_recognition_fence(tmp_path: Path) -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def handoff(_ticket: Ticket) -> None:
            await release.wait()

        async with Harness(tmp_path, handoff=handoff) as run:
            try:
                await run.start(profile='bind')
                await run.runtime.wait_for(
                    lambda: isinstance(run.runtime.state, Starting)
                    and run.runtime.state.provisional is not None
                )
                run.close_lease()
                await run.runtime.wait_for(lambda: isinstance(run.runtime.state, Terminating))
                release.set()
                assert run.task is not None
                await run.task
                assert run.runtime.committed['ATTEMPT'] == 1
                assert set(run.runtime.tickets) == {1}
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize('dispatch_exit_first', (False, True))
def test_natural_exit_shutdown_race_allows_either_cause(
    tmp_path: Path,
    dispatch_exit_first: bool,
) -> None:
    async def scenario() -> None:
        from .model import Exited

        async with Harness(tmp_path) as run:
            await run.start()
            await run.ready()
            ticket = run.current_ticket()
            assert ticket.scope is not None
            asyncio.get_running_loop().remove_reader(ticket.scope.pidfd)
            await run.command('exit', returncode=4)
            await readable(ticket.scope.pidfd)
            genuine = ticket.scope.poll()
            assert genuine == 4
            run.close_lease()
            if dispatch_exit_first:
                run.runtime.admit(Exited(1, genuine))
                run.runtime.observe_control()
            else:
                run.runtime.observe_control()
                run.runtime.admit(Exited(1, genuine))
            assert run.task is not None
            closed = await run.task
            assert closed.outcome.trigger in ('process-exit', 'controller-ended')
            assert closed.outcome.child == ChildResult(1, 4)
            assert run.runtime.committed['SESSION_ENDED'] == 1
            assert not closed.residuals

    asyncio.run(scenario())


def test_failed_spawn_still_exposes_exact_attempt_argv(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            argv = [str(tmp_path / 'nonexistent-executable'), '--literal-argument']
            await write(
                run.control_write,
                (
                    json.dumps(
                        {
                            'type': 'START',
                            'argv': argv,
                            'required': [],
                            'policy': 'live',
                            'max_attempts': 1,
                        }
                    )
                    + '\n'
                ).encode(),
            )
            assert run.task is not None
            closed = await run.task
            attempt = await run.frame('ATTEMPT')
            assert attempt['argv'] == argv
            assert closed.outcome.primary is not None
            assert closed.outcome.child is None
            assert run.runtime.tickets[1].settled
            assert not closed.residuals

    asyncio.run(scenario())


def test_markers_completed_at_real_stream_eof(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            await run.emit('stdout', b'INIT')
            await run.emit('stderr', b'STARTUP')
            await run.command('close-output')
            await run.frame('READY')
            assert isinstance(run.runtime.state, Active)
            await run.finish()

    asyncio.run(scenario())


def test_retry_before_authority_observes_real_eof_is_permitted(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start(profile='bind')
            asyncio.get_running_loop().remove_reader(run.control_read)
            run.close_lease()  # real EOF exists but is not an authority decision
            await run.receipt_kind('spawned', generation=2)
            assert run.runtime.committed['ATTEMPT'] == 2
            assert run.runtime.committed['SESSION_ENDED'] == 0
            run.runtime.observe_control()
            assert run.task is not None
            closed = await run.task
            assert not closed.residuals
            assert all(ticket.settled for ticket in run.runtime.tickets.values())

    asyncio.run(scenario())


def test_accounted_eof_defeats_ready_candidates(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            for stream in run.runtime.streams.values():
                stream.pause()
            await run.emit('stdout', b'INIT\n')
            await run.emit('stderr', b'STARTUP\n')
            # These are actual retained observations, not fabricated readiness facts.
            for stream in run.runtime.streams.values():
                stream.final_scan()
            run.close_lease()
            run.runtime.observe_control()
            assert run.task is not None
            closed = await run.task
            assert run.runtime.committed['READY'] == 0
            assert not closed.residuals

    asyncio.run(scenario())


def test_native_path_diagnostic_serialization_cannot_abandon_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            argv = [str(tmp_path / 'missing-\udcff')]
            await write(
                run.control_write,
                (
                    json.dumps(
                        {
                            'type': 'START',
                            'argv': argv,
                            'required': [],
                            'policy': 'live',
                            'max_attempts': 1,
                        }
                    )
                    + '\n'
                ).encode(),
            )
            assert run.task is not None
            closed = await run.task
            assert (await run.frame('ATTEMPT'))['argv'] == argv
            assert closed.outcome.primary is not None
            assert closed.outcome.child is None
            assert not run.workspace.path.exists() and not closed.residuals

    asyncio.run(scenario())
