# SPDX-License-Identifier: Apache-2.0
"""Deterministic test driver at real child/pipe/clock seams; no lifecycle algorithm."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from .model import Active, Closed, Owned, Starting
from .runtime import Runtime
from .unix import ManualTimers, Subreaper, Ticket, readable
from .workspace import Workspace

STANDIN = Path(__file__).with_name('standin.py')


async def write(descriptor: int, data: bytes) -> None:
    os.set_blocking(descriptor, False)
    view = memoryview(data)
    loop = asyncio.get_running_loop()
    while view:
        try:
            count = os.write(descriptor, view)
        except BlockingIOError:
            future: asyncio.Future[None] = loop.create_future()

            def writable(cell: asyncio.Future[None] = future) -> None:
                if not cell.done():
                    cell.set_result(None)

            loop.add_writer(descriptor, writable)
            try:
                await future
            finally:
                loop.remove_writer(descriptor)
        else:
            view = view[count:]


class JsonReader:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.buffer = bytearray()
        os.set_blocking(descriptor, False)

    async def read(self) -> dict[str, object]:
        while b'\n' not in self.buffer:
            try:
                data = os.read(self.descriptor, 65536)
            except BlockingIOError:
                await readable(self.descriptor)
                continue
            if not data:
                raise EOFError('experimental peer closed')
            self.buffer.extend(data)
        line, rest = self.buffer.split(b'\n', 1)
        self.buffer = bytearray(rest)
        result: dict[str, object] = json.loads(line)
        return result


class Harness:
    def __init__(
        self,
        parent: Path,
        *,
        drain: bool = True,
        handoff: Callable[[Ticket], Awaitable[None]] | None = None,
        interrupt: Callable[[str, Ticket], None] | None = None,
        stage_released: int | None = None,
    ) -> None:
        self.control_read, self.control_write = os.pipe()
        self.output_read, self.output_write = os.pipe()
        self.command_read, self.command_write = os.pipe()
        self.receipt_read, self.receipt_write = os.pipe()
        self.timers = ManualTimers()
        self.workspace = Workspace(parent / 'workspace', stage_released)
        self.runtime = Runtime(
            self.control_read,
            self.output_write,
            self.workspace,
            timers=self.timers,
            pass_fds=(self.command_read, self.receipt_write),
            handoff=handoff,
            interrupt=interrupt,
        )
        self.draining = drain
        self.frames: list[dict[str, object]] = []
        self.receipts: list[dict[str, object]] = []
        self.frames_changed = asyncio.Condition()
        self.output = JsonReader(self.output_read)
        self.receipt = JsonReader(self.receipt_read)
        self.task: asyncio.Task[Closed] | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.reaper = Subreaper()
        self.leases_closed = False
        self.saved: list[Ticket] = []
        self.argv: tuple[str, ...] = ()

    async def __aenter__(self) -> Harness:
        self.task = asyncio.create_task(self.runtime.run())
        if self.draining:
            self.reader_task = asyncio.create_task(self.read_output())
            await self.frame('SESSION_CREATED')
        return self

    async def read_output(self) -> None:
        while True:
            frame = await self.output.read()
            self.frames.append(frame)
            async with self.frames_changed:
                self.frames_changed.notify_all()

    async def discard_output(self) -> None:
        # Fixture disposal tolerates intentionally partial/malformed peer bytes.
        while True:
            try:
                chunk = os.read(self.output_read, 65536)
            except BlockingIOError:
                await readable(self.output_read)
                continue
            if not chunk:
                return

    async def frame(self, kind: str) -> dict[str, object]:
        async with self.frames_changed:
            await self.frames_changed.wait_for(
                lambda: any(frame['type'] == kind for frame in self.frames)
            )
        return next(frame for frame in self.frames if frame['type'] == kind)

    async def receipt_kind(self, kind: str, *, generation: int | None = None) -> dict[str, object]:
        def matches(item: dict[str, object]) -> bool:
            return item['kind'] == kind and (
                generation is None or item.get('generation') == generation
            )

        while not any(matches(item) for item in self.receipts):
            self.receipts.append(await self.receipt.read())
        return next(item for item in self.receipts if matches(item))

    async def send_start(
        self,
        *,
        profile: str = 'controlled',
        policy: str = 'live',
        required: tuple[str, ...] = ('INIT', 'STARTUP'),
    ) -> None:
        self.argv = (
            sys.executable,
            '-u',
            str(STANDIN),
            '--commands',
            str(self.command_read),
            '--receipts',
            str(self.receipt_write),
            '--profile',
            profile,
        )
        await write(
            self.control_write,
            (
                json.dumps(
                    {
                        'type': 'START',
                        'argv': self.argv,
                        'required': required,
                        'policy': policy,
                        'max_attempts': 2,
                    }
                )
                + '\n'
            ).encode(),
        )

    async def start(
        self,
        *,
        profile: str = 'controlled',
        policy: str = 'live',
        required: tuple[str, ...] = ('INIT', 'STARTUP'),
    ) -> None:
        await self.send_start(profile=profile, policy=policy, required=required)
        await self.receipt_kind('spawned', generation=1)
        if 1 in self.runtime.tickets:
            self.saved.append(self.runtime.tickets[1])

    async def command(self, kind: str, **fields: object) -> None:
        previous = len(self.receipts)
        await write(self.command_write, (json.dumps({'kind': kind, **fields}) + '\n').encode())
        expected = 'exiting' if kind == 'exit' else 'ack'
        while not any(item['kind'] == expected for item in self.receipts[previous:]):
            self.receipts.append(await self.receipt.read())

    async def emit(self, stream: str, data: bytes) -> None:
        await self.command('write', stream=stream, hex=data.hex())

    async def ready(self) -> None:
        await self.emit('stdout', b'INIT\n')
        await self.emit('stderr', b'STARTUP\n')
        await self.frame('READY')

    def close_lease(self) -> None:
        if not self.leases_closed:
            os.close(self.control_write)
            self.leases_closed = True

    def current_ticket(self) -> Ticket:
        state = self.runtime.state
        if isinstance(state, Active):
            return state.child.ticket
        assert isinstance(state, Starting) and isinstance(state.attempt, Owned)
        return state.attempt.ticket

    async def finish(self) -> Closed:
        self.close_lease()
        assert self.task is not None
        return await self.task

    async def __aexit__(self, _kind: object, _failure: object, _traceback: object) -> None:
        self.close_lease()
        # Tests release controllable foreign producers before exiting their scope.
        try:
            if self.task is not None and not self.task.done():
                self.runtime.changed.set()
                if _failure is not None or self.reader_task is None or self.reader_task.done():
                    if self.reader_task is not None:
                        self.reader_task.cancel()
                        await asyncio.gather(self.reader_task, return_exceptions=True)
                    self.reader_task = asyncio.create_task(self.discard_output())
                await self.task
        finally:
            await self.rescue()

    async def rescue(self) -> None:
        """Independent fixture disposal, including deliberately broken variants."""
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        # Negative mutations must not leak real processes out of the test.
        tickets = {id(ticket): ticket for ticket in (*self.saved, *self.runtime.tickets.values())}
        for ticket in tickets.values():
            if ticket.scope is not None and not ticket.scope.descriptors_closed:
                asyncio.get_running_loop().remove_reader(ticket.scope.pidfd)
            if ticket.process is not None and ticket.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(ticket.process.pid, signal.SIGKILL)
                ticket.process.wait(timeout=30)
                for stream in (ticket.process.stdout, ticket.process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            if ticket.scope is not None:
                ticket.scope.close_descriptors()
        descriptors = (
            self.control_read,
            self.output_read,
            self.output_write,
            self.command_read,
            self.command_write,
            self.receipt_read,
            self.receipt_write,
        )
        for descriptor in descriptors:
            os.close(descriptor)
        self.reaper.close()
