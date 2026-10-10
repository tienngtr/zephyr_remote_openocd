# SPDX-License-Identifier: Apache-2.0
"""Actual Unix output, local launch and independent workspace boundaries."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess

from .harness import STANDIN, Harness, JsonReader, write
from .local import Cancelling, LaunchGate, shutdown
from .model import (
    Closed,
    Diagnostic,
    Failure,
    TerminalResult,
    failure_record,
)
from .unix import ByteWriter, ManualTimers, readable


def fill(descriptor: int) -> None:
    os.set_blocking(descriptor, False)
    while True:
        try:
            os.write(descriptor, b'x' * 4096)
        except BlockingIOError:
            return


def test_local_launch_revalidates_cancellation() -> None:
    async def scenario() -> None:
        gate = LaunchGate()
        gate.ready()
        gate.forwarded()
        spawned: list[subprocess.Popen[bytes]] = []

        def spawn() -> subprocess.Popen[bytes]:
            child = subprocess.Popen([sys.executable, '-c', 'pass'])
            spawned.append(child)
            return child

        launch = gate.queue_launch(spawn)
        gate.cancel()
        gate.ready()  # permitted late remote READY
        try:
            assert not await launch
            assert isinstance(gate.state, Cancelling)
            assert not spawned
        finally:
            for child in spawned:
                child.wait(timeout=30)

    asyncio.run(scenario())


@pytest.mark.parametrize('ready,forwarded', [(False, True), (True, False), (True, True)])
def test_local_launch_requires_both(ready: bool, forwarded: bool) -> None:
    async def scenario() -> None:
        gate = LaunchGate()
        if ready:
            gate.ready()
        if forwarded:
            gate.forwarded()
        spawned: list[subprocess.Popen[bytes]] = []

        def spawn() -> subprocess.Popen[bytes]:
            child = subprocess.Popen([sys.executable, '-c', 'pass'])
            spawned.append(child)
            return child

        try:
            assert await gate.queue_launch(spawn) == (ready and forwarded)
        finally:
            for child in spawned:
                child.wait(timeout=30)

    asyncio.run(scenario())


def test_real_partial_write_fifo_and_bounded_buffer() -> None:
    async def scenario() -> None:
        reader, output = os.pipe()
        writer = ByteWriter(output, capacity=262144)
        payloads: list[dict[str, object]] = [
            {'type': 'CHILD_OUTPUT', 'text': char * 50000} for char in ('a', 'b')
        ]
        peer = JsonReader(reader)
        try:
            assert all(writer.admit(frame) for frame in payloads)
            writer.flush()  # actual pipe capacity forces a short write
            assert writer.partial_writes > 0
            assert not writer.admit({'type': 'CHILD_OUTPUT', 'text': 'z' * 300000})
            assert [await peer.read(), await peer.read()] == payloads
            assert await writer.drain(ManualTimers()) is None
            assert writer.pending == 0
        finally:
            writer.close()
            os.close(reader)
            os.close(output)

    asyncio.run(scenario())


def test_attempt_admission_failure_prevents_real_spawn(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            run.runtime.writer.capacity = 1
            await run.send_start()
            assert run.task is not None
            closed = await run.task
            assert not run.runtime.tickets
            assert run.runtime.committed['ATTEMPT'] == 0
            assert closed.outcome.primary is not None
            assert closed.outcome.primary.source == 'writer'

    asyncio.run(scenario())


def test_ready_admission_failure_prevents_active(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            run.runtime.writer.capacity = 1
            await run.emit('stdout', b'INIT\n')
            await run.emit('stderr', b'STARTUP\n')
            assert run.task is not None
            closed = await run.task
            assert run.runtime.committed['READY'] == 0
            assert not closed.residuals
            assert closed.outcome.primary is not None
            assert closed.outcome.primary.source == 'writer'

    asyncio.run(scenario())


@pytest.mark.parametrize('partial', [False, True])
def test_terminal_writer_failure_no_second_result(tmp_path: Path, partial: bool) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            ticket = run.current_ticket()
            assert run.reader_task is not None
            run.reader_task.cancel()
            await asyncio.gather(run.reader_task, return_exceptions=True)
            primary = Diagnostic('operation', 'original', (Diagnostic('detail', 'nested'),))
            if not partial:
                fill(run.output_write)
            run.runtime.terminate('failure', primary)
            if partial:
                # A large immutable terminal frame exercises partial physical writes.
                run.runtime.terminate('failure', Diagnostic('cleanup', 'detail' * 30000))
            await run.runtime.wait_for(lambda: isinstance(run.runtime.state, Closed))
            if partial:
                run.runtime.writer.flush()
                assert run.runtime.writer.partial_writes > 0
            os.close(run.output_read)
            # Replace the closed fixture-owned FD; the writer's real original reader
            # is gone, yielding EPIPE on its next write, including final drain.
            run.output_read = os.open('/dev/null', os.O_RDONLY)
            run.runtime.writer.flush()
            assert run.task is not None
            closed = await run.task
            assert closed.outcome.primary == primary
            assert run.runtime.committed['SESSION_ENDED'] == 1
            assert ticket.scope is not None and ticket.scope.closed
            assert any(item.source == 'writer' for item in closed.outcome.diagnostics)
            assert run.runtime.terminal_snapshot is not None
            assert run.runtime.terminal_snapshot.primary == primary

    asyncio.run(scenario())


def test_output_backpressure_does_not_hold_process_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            ticket = run.current_ticket()
            assert run.reader_task is not None
            run.reader_task.cancel()
            await asyncio.gather(run.reader_task, return_exceptions=True)
            fill(run.output_write)
            run.close_lease()
            await run.timers.wait_armed('output-drain')
            assert ticket.settled and ticket.scope is not None and ticket.scope.closed
            assert not run.workspace.path.exists()
            run.timers.expire('output-drain')
            assert run.task is not None
            closed = await run.task
            assert run.runtime.committed['SESSION_ENDED'] == 1
            assert closed.outcome.primary is not None
            assert closed.outcome.primary.source == 'writer'

    asyncio.run(scenario())


def test_authority_task_cancellation_keeps_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            ticket = run.current_ticket()
            assert run.task is not None
            run.task.cancel()
            closed = await run.task
            assert closed.outcome.trigger == 'cancelled'
            assert ticket.scope is not None and ticket.scope.closed
            assert ticket.settled and not closed.residuals

    asyncio.run(scenario())


def test_cross_process_staging_excludes_workspace_removal(tmp_path: Path) -> None:
    async def scenario() -> None:
        release_read, release_write = os.pipe()
        gate_read, gate_write = os.pipe()
        receipt_read, receipt_write = os.pipe()
        stage: subprocess.Popen[bytes] | None = None
        try:
            async with Harness(tmp_path, stage_released=release_read) as run:
                code = (
                    'import fcntl,os,pathlib,sys\n'
                    'lease=open(sys.argv[1],"r+b")\n'
                    'fcntl.flock(lease,fcntl.LOCK_SH)\n'
                    'os.write(int(sys.argv[3]),b"held")\n'
                    'os.read(int(sys.argv[2]),1)\n'
                    'assert pathlib.Path(sys.argv[5]).is_dir()\n'
                    'os.write(int(sys.argv[3]),b"validated")\n'
                    'fcntl.flock(lease,fcntl.LOCK_UN)\n'
                    'os.write(int(sys.argv[4]),b"r")\n'
                )
                stage = subprocess.Popen(
                    [
                        sys.executable,
                        '-c',
                        code,
                        str(run.workspace.lease),
                        str(gate_read),
                        str(receipt_write),
                        str(release_write),
                        str(run.workspace.path),
                    ],
                    pass_fds=(gate_read, receipt_write, release_write),
                )
                await readable(receipt_read)
                assert os.read(receipt_read, 4) == b'held'
                await run.start()
                ticket = run.current_ticket()
                run.close_lease()
                await run.timers.wait_armed('workspace')
                await run.runtime.wait_for(lambda: ticket.settled)
                assert run.workspace.path.is_dir()
                assert ticket.scope is not None and ticket.scope.closed
                with pytest.raises(ValueError), run.workspace.staging():
                    pass
                os.write(gate_write, b'g')
                assert run.task is not None
                closed = await run.task
                assert not closed.residuals and not run.workspace.path.exists()
                assert stage.wait(timeout=30) == 0
                await readable(receipt_read)
                assert os.read(receipt_read, 9) == b'validated'
        finally:
            if stage is not None and stage.poll() is None:
                stage.kill()
                stage.wait(timeout=30)
            for descriptor in (
                release_read,
                release_write,
                gate_read,
                gate_write,
                receipt_read,
                receipt_write,
            ):
                os.close(descriptor)

    asyncio.run(scenario())


def test_local_timeout_does_not_claim_remote_cleanup() -> None:
    async def scenario() -> None:
        receipt_read, receipt_write = os.pipe()
        command_read, command_write = os.pipe()
        process = subprocess.Popen(
            [
                sys.executable,
                '-u',
                str(STANDIN),
                '--commands',
                str(command_read),
                '--receipts',
                str(receipt_write),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(command_read, receipt_write),
            env={**os.environ, 'ZRO_LEASE_GENERATION': '1'},
        )
        managed = ManagedSshProcess.from_popen(process)
        timers = ManualTimers()
        terminal: asyncio.Future[TerminalResult | None] = asyncio.get_running_loop().create_future()
        receipts = JsonReader(receipt_read)
        try:
            assert (await receipts.read())['kind'] == 'spawned'
            await write(command_write, b'{"kind":"ignore-term"}\n')
            assert (await receipts.read())['kind'] == 'ack'
            task = asyncio.create_task(shutdown(managed, process.pid, terminal, timers=timers))
            await timers.wait_armed('local-shutdown')
            timers.expire('local-shutdown')
            assert (await receipts.read())['kind'] == 'signal'
            await timers.wait_armed('local-term')
            timers.expire('local-term')
            result = await task
            assert not result.remote_cleanup_confirmed
            assert not result.terminal_received
            assert result.transport_status == -signal.SIGKILL
            assert result.outcome.child is None
            assert result.outcome.primary is not None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            managed.close_stderr()
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
            for descriptor in (receipt_read, receipt_write, command_read, command_write):
                os.close(descriptor)

    asyncio.run(scenario())


def test_writer_failure_after_spawn_keeps_resource_owned(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            ticket = run.current_ticket()
            assert run.reader_task is not None
            run.reader_task.cancel()
            await asyncio.gather(run.reader_task, return_exceptions=True)
            os.close(run.output_read)
            run.output_read = os.open('/dev/null', os.O_RDONLY)
            await run.emit('stdout', b'actual child output\n')
            assert run.task is not None
            closed = await run.task
            assert ticket.process is not None and ticket.process.returncode is not None
            assert ticket.scope is not None and ticket.scope.closed
            assert closed.outcome.primary is not None
            assert closed.outcome.primary.source == 'writer'
            assert run.runtime.committed['SESSION_ENDED'] == 1
            assert run.runtime.writer.failure is not None
            assert not closed.residuals

    asyncio.run(scenario())


def test_staging_timeout_leaves_workspace_and_cleans_child(tmp_path: Path) -> None:
    async def scenario() -> None:
        notification, notify = os.pipe()
        try:
            async with Harness(tmp_path, stage_released=notification) as run:
                with run.workspace.staging():
                    await run.start()
                    ticket = run.current_ticket()
                    run.close_lease()
                    await run.timers.wait_armed('workspace')
                    await run.runtime.wait_for(lambda: ticket.settled)
                    run.timers.expire('workspace')
                    assert run.task is not None
                    closed = await run.task
                    assert run.workspace.path.is_dir()
                    assert ticket.scope is not None and ticket.scope.closed
                    assert 'workspace' in closed.residuals
                    assert closed.outcome.primary is not None
                    assert closed.outcome.primary.source == 'cleanup'
                    final = await run.frame('SESSION_ENDED')
                    assert final['disposal_confirmed'] is False
        finally:
            os.close(notification)
            os.close(notify)

    asyncio.run(scenario())


def test_secondary_nested_cleanup_details_survive_terminal_writer_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            assert run.reader_task is not None
            run.reader_task.cancel()
            await asyncio.gather(run.reader_task, return_exceptions=True)
            fill(run.output_write)
            primary = Diagnostic('operation', 'established')
            secondary = None
            try:
                (tmp_path / 'unavailable-cleanup-target').unlink()
            except OSError as error:
                error.add_note('retained nested cleanup diagnostic')
                secondary = failure_record('cleanup', error)
            assert secondary is not None
            run.runtime.terminate('failure', primary)
            run.runtime.admit(Failure(secondary))
            await run.runtime.wait_for(lambda: isinstance(run.runtime.state, Closed))
            os.close(run.output_read)
            run.output_read = os.open('/dev/null', os.O_RDONLY)
            run.runtime.writer.flush()
            assert run.task is not None
            closed = await run.task
            assert closed.outcome.primary == primary
            assert secondary in closed.outcome.diagnostics
            assert closed.outcome.diagnostics[0].details == secondary.details
            assert any(value.source == 'writer' for value in closed.outcome.diagnostics)
            assert run.runtime.committed['SESSION_ENDED'] == 1

    asyncio.run(scenario())


def test_real_late_remote_ready_is_benign_after_local_cancel(tmp_path: Path) -> None:
    async def scenario() -> None:
        gate = LaunchGate()
        gate.forwarded()
        spawned: list[subprocess.Popen[bytes]] = []

        def spawn() -> subprocess.Popen[bytes]:
            child = subprocess.Popen([sys.executable, '-c', 'pass'])
            spawned.append(child)
            return child

        try:
            async with Harness(tmp_path) as run:
                await run.start()
                gate.cancel()
                await run.ready()  # actual remote subprocess marker decoding and READY
                gate.ready()
                assert not await gate.queue_launch(spawn)
                assert not spawned
                closed = await run.finish()
                assert run.runtime.committed['READY'] == 1
                assert not closed.residuals
        finally:
            for child in spawned:
                child.wait(timeout=30)

    asyncio.run(scenario())


def test_active_local_cancel_retains_process_until_actual_settlement() -> None:
    async def scenario() -> None:
        from .model import Outcome

        gate = LaunchGate()
        gate.ready()
        gate.forwarded()
        child: subprocess.Popen[bytes] | None = None

        def spawn() -> subprocess.Popen[bytes]:
            nonlocal child
            child = subprocess.Popen(
                [
                    sys.executable,
                    '-c',
                    'import sys; sys.stdin.buffer.read()',
                ],
                stdin=subprocess.PIPE,
            )
            return child

        try:
            assert await gate.queue_launch(spawn)
            gate.cancel(Diagnostic('local', 'established'))
            assert isinstance(gate.state, Cancelling) and gate.state.process is child
            with pytest.raises(ValueError):
                gate.finish(Outcome('requested'))
            assert child is not None and child.stdin is not None
            child.stdin.close()
            descriptor = os.pidfd_open(child.pid)
            try:
                await readable(descriptor)
            finally:
                os.close(descriptor)
            gate.finish(Outcome('requested', Diagnostic('cleanup', 'secondary')))
            gate.ready()
            gate.forwarded()
            assert not await gate.queue_launch(spawn)
        finally:
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=30)
                if child.stdin is not None:
                    child.stdin.close()

    asyncio.run(scenario())


def test_final_bulk_backpressure_uses_budget_after_resources_settle(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Harness(tmp_path) as run:
            await run.start()
            await run.ready()
            ticket = run.current_ticket()
            assert run.reader_task is not None
            run.reader_task.cancel()
            await asyncio.gather(run.reader_task, return_exceptions=True)
            run.runtime.writer.capacity = 40000
            payload = b'x' * 130000
            await run.emit('stdout', payload)
            await run.command('exit', returncode=0)
            await run.timers.wait_armed('output-drain')
            await run.runtime.wait_for(lambda: ticket.settled)
            assert ticket.scope is not None and ticket.scope.closed
            assert run.runtime.committed['SESSION_ENDED'] == 0
            assert not run.workspace.path.exists()
            run.reader_task = asyncio.create_task(run.read_output())
            assert run.task is not None
            closed = await run.task
            assert closed.outcome.primary is None and not closed.residuals
            await run.frame('SESSION_ENDED')
            stdout = ''.join(
                str(frame['text'])
                for frame in run.frames
                if frame['type'] == 'CHILD_OUTPUT' and frame['stream'] == 'stdout'
            )
            assert stdout.endswith(payload.decode())
            assert run.runtime.committed['SESSION_ENDED'] == 1
            assert all(stream.peak <= stream.limit for stream in run.runtime.streams.values())

    asyncio.run(scenario())
