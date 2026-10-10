# SPDX-License-Identifier: Apache-2.0
"""Controlled physical seams only: no fake lifecycle algorithm or real I/O."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .contracts import Diagnostic, EffectFailure, Owner, ResourceOwner


class Gate:
    def __init__(self, *, open_initially: bool = False) -> None:
        self.event = asyncio.Event()
        if open_initially:
            self.event.set()

    def release(self) -> None:
        self.event.set()

    async def wait(self) -> None:
        await self.event.wait()


class ControlledHandle:
    def __init__(self, scope: ResourceOwner, cleanup_failure: Diagnostic | None) -> None:
        self.scope = scope
        self.cleanup_failure = cleanup_failure
        self.live = True
        self.cleaning = asyncio.Event()
        self.cleanup_gate = Gate(open_initially=True)
        self.cleaned_by: list[Owner] = []
        self.on_cleanup: Callable[[], None] = lambda: None

    async def dispose(self) -> None:
        assert self.live and not self.cleaned_by, 'independent duplicate cleanup'
        self.cleaned_by.append(self.scope.owner)
        self.cleaning.set()
        await self.cleanup_gate.wait()
        if self.cleanup_failure is not None:
            raise EffectFailure(self.cleanup_failure)
        self.live = False
        self.on_cleanup()  # final physical response seam, before lifecycle accounting
        self.scope.changed()


class ControlledAcquisition:
    def __init__(
        self,
        *,
        pause_entry: bool = False,
        pause_transfer: bool = False,
        failure: Diagnostic | None = None,
        retryable: bool = False,
        cleanup_failure: Diagnostic | None = None,
        transfer_interrupt: str | None = None,
        ignore_entry_cancellation: bool = False,
        begin_failure: BaseException | None = None,
    ) -> None:
        self.entry_gate = Gate(open_initially=not pause_entry)
        self.completion_gate = Gate()
        self.transfer_gate = Gate(open_initially=not pause_transfer)
        self.entered = asyncio.Event()
        self.entry_returned = asyncio.Event()
        self.at_entry = asyncio.Event()
        self.acquired = asyncio.Event()
        self.at_transfer = asyncio.Event()
        self.failure = failure
        self.retryable = retryable
        self.cleanup_failure = cleanup_failure
        self.transfer_interrupt = transfer_interrupt
        self.ignore_entry_cancellation = ignore_entry_cancellation
        self.begin_failure = begin_failure
        self.handle: ControlledHandle | None = None

    async def produce(self, scope: ResourceOwner) -> None:
        await self.completion_gate.wait()
        if self.failure is not None:
            raise EffectFailure(self.failure, retryable=self.retryable)
        self.handle = ControlledHandle(scope, self.cleanup_failure)
        scope.bind(self.handle)  # physical scope already designated before entry
        self.acquired.set()

    async def before_transfer(self) -> None:
        self.at_transfer.set()
        await self.transfer_gate.wait()

    def transfer(self, publish: Callable[[], None]) -> None:
        if self.transfer_interrupt == 'before':
            raise asyncio.CancelledError('before ownership publication')
        publish()
        if self.transfer_interrupt == 'after':
            raise asyncio.CancelledError('after ownership publication')

    def release_all(self) -> None:
        self.entry_gate.release()
        self.completion_gate.release()
        self.transfer_gate.release()
        if self.handle is not None:
            self.handle.cleanup_gate.release()


class ControlledEffects:
    def __init__(self, *plans: ControlledAcquisition, pause_receive: bool = False) -> None:
        self.plans = plans
        self.started: list[int] = []
        self.receive_gate = Gate(open_initially=not pause_receive)

    async def before_receive(self) -> None:
        await self.receive_gate.wait()

    async def before_entry(self, generation: int) -> None:
        plan = self.plans[generation - 1]
        plan.at_entry.set()
        while not plan.entry_gate.event.is_set():
            try:
                await plan.entry_gate.wait()
            except asyncio.CancelledError:
                if not plan.ignore_entry_cancellation:
                    raise
        plan.entry_returned.set()

    def begin(self, generation: int, scope: ResourceOwner) -> ControlledAcquisition:
        plan = self.plans[generation - 1]
        self.started.append(generation)
        plan.entered.set()
        if plan.begin_failure is not None:
            plan.handle = ControlledHandle(scope, plan.cleanup_failure)
            scope.bind(plan.handle)
            raise plan.begin_failure
        return plan


class ControlledSink:
    """A write accepts a selected prefix or fails; no claim about peer receipt."""

    def __init__(self, *, automatic: bool = True) -> None:
        self.automatic = automatic
        self.steps: asyncio.Queue[int | Diagnostic] = asyncio.Queue(32)
        self.changed = asyncio.Event()
        self.requests: list[bytes] = []
        self.wire = bytearray()

    def advance(self, count: int = 4096) -> None:
        self.steps.put_nowait(count)

    def fail(self, failure: Diagnostic) -> None:
        self.steps.put_nowait(failure)

    def unblock(self) -> None:
        self.automatic = True
        self.advance()

    async def write(self, data: bytes) -> int:
        self.requests.append(data)
        self.changed.set()
        step = len(data) if self.automatic else await self.steps.get()
        if isinstance(step, Diagnostic):
            raise EffectFailure(step)
        count = min(step, len(data))
        self.wire.extend(data[:count])
        self.changed.set()
        return count

    async def wait_for(self, predicate: Callable[[ControlledSink], bool]) -> None:
        while not predicate(self):
            self.changed.clear()
            if predicate(self):
                break
            await self.changed.wait()
