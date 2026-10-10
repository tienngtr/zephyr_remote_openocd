# SPDX-License-Identifier: Apache-2.0
"""Bounded admission, two real producer patterns, and a native-signal seam."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

from .contracts import ControlBatch, Diagnostic, Fact, Kind, diagnostic_from

type Observation = Fact | ControlBatch


class Admission:
    """Queue credit and recognition are serialized by the one event loop."""

    def __init__(self, capacity: int, wake: asyncio.Event) -> None:
        if capacity < 1:
            raise ValueError('admission must be bounded')
        self.queue: asyncio.Queue[Observation] = asyncio.Queue(capacity)
        self.space = asyncio.Event()
        self.backpressured = asyncio.Event()  # observable experiment handshake, not decision state
        self.wake = wake

    async def publish(self, recognize: Callable[[], Observation]) -> None:
        while self.queue.full():
            self.space.clear()
            self.backpressured.set()
            await self.space.wait()
        # No await from available credit through conversion and admission.
        self.publish_nowait(recognize)

    def publish_nowait(self, recognize: Callable[[], Observation]) -> None:
        if self.queue.full():
            raise asyncio.QueueFull
        self.queue.put_nowait(recognize())
        self.wake.set()

    def take(self) -> Observation:
        observation = self.queue.get_nowait()
        self.space.set()
        return observation


class Strategy(Enum):
    ATOMIC = 'atomic'
    PREFIX = 'prefix'


@dataclass
class SuccessBarrier:
    epoch: int
    kind: str
    generation: int
    acknowledged: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)


class ControlAdapter:
    """A bounded fake read seam with genuine async bounded publication.

    Input syntax is deliberately experimental LF-delimited START/STOP tokens,
    not the production JSON decoder. PREFIX recognizes in accept_read(), a
    separate producer callback; ATOMIC converts only under admission credit.
    One retained original batch plus one unparsed read is the prefix bound.
    """

    MAX_READ_BYTES = 1024
    MAX_FRAMES = 32

    def __init__(self, strategy: Strategy, admission: Admission) -> None:
        self.strategy = strategy
        self.admission = admission
        self.available = asyncio.Event()
        self.reads: deque[bytes | None] = deque()
        self.original: ControlBatch | None = None
        self.buffer = b''
        self.recognized = 0
        self.admitted = 0
        self.barrier: SuccessBarrier | None = None
        self.closing = False
        self.finished = False

    def accept_read(self, chunk: bytes | None) -> None:
        """Caller retains the read if bounded local acceptance fails."""
        if self.closing:
            return
        if chunk is not None and len(chunk) > self.MAX_READ_BYTES:
            raise BufferError('fake read exceeds bounded source capacity')
        if self.strategy == Strategy.PREFIX and self.original is None and self.barrier is None:
            self.original = self._recognize(chunk)
        else:
            if self.reads:
                raise BufferError('one unread batch already retained')
            self.reads.append(chunk)
        self.available.set()

    def _recognize(self, chunk: bytes | None) -> ControlBatch:
        facts: tuple[Fact, ...]
        if chunk is None:
            facts = (Fact(Kind.INVALID, failure=Diagnostic('protocol', 'incomplete frame')),)
            if not self.buffer:
                facts = (Fact(Kind.EOF),)
            self.buffer = b''
        else:
            self.buffer += chunk
            lines = self.buffer.split(b'\n')
            self.buffer = lines.pop()
            if len(self.buffer) > self.MAX_READ_BYTES or len(lines) > self.MAX_FRAMES:
                self.buffer = b''
                facts = (Fact(Kind.INVALID, failure=Diagnostic('protocol', 'frame bound')),)
            else:
                facts = tuple(
                    Fact(Kind.START if line == b'START' else Kind.STOP)
                    if line in (b'START', b'STOP')
                    else Fact(Kind.INVALID, failure=Diagnostic('protocol', 'invalid frame'))
                    for line in lines
                )
        self.recognized += len(facts)
        return ControlBatch(facts)

    def request(self, barrier: SuccessBarrier) -> None:
        assert self.strategy == Strategy.PREFIX and self.barrier is None
        self.barrier = barrier  # recognition hold starts here, before any await
        if self.finished:
            barrier.acknowledged.set()
        self.available.set()

    def release(self, barrier: SuccessBarrier) -> None:
        if self.barrier is barrier:
            self.barrier = None
        barrier.released.set()
        self.available.set()

    def stop(self) -> None:
        self.closing = True
        self.reads.clear()  # unread/unparsed input has not become a fact
        self.available.set()

    def recover_original(self) -> ControlBatch | None:
        """Joining an interrupted producer accounts its original, never a copy."""
        batch, self.original = self.original, None
        return batch

    def _retained(self) -> ControlBatch:
        assert self.original is not None
        return self.original

    def _consume_atomic(self) -> ControlBatch:
        # Even the EOF result is not examined/consumed before credit exists.
        if self.closing:
            return ControlBatch(())
        return self._recognize(self.reads.popleft())

    async def run(self) -> Diagnostic | None:
        try:
            while True:
                if self.original is not None:
                    batch = self.original
                    await self.admission.publish(self._retained)
                    self.admitted += len(batch.facts)
                    self.original = None
                    continue
                if self.barrier is not None:
                    barrier = self.barrier
                    assert self.recognized == self.admitted
                    barrier.acknowledged.set()
                    self.admission.wake.set()
                    await barrier.released.wait()
                    continue
                if self.closing:
                    return None
                if self.reads:
                    if self.strategy == Strategy.ATOMIC:
                        await self.admission.publish(self._consume_atomic)
                        self.admitted = self.recognized
                    else:
                        self.original = self._recognize(self.reads.popleft())
                    continue
                self.available.clear()
                await self.available.wait()
        except Exception as exception:
            return diagnostic_from(exception)
        finally:
            self.finished = True
            self.admission.wake.set()


class NativeSignalLatch:
    """The only simulated native-handler state: a first-signal integer latch."""

    def __init__(self) -> None:
        self.signum: int | None = None

    def capture(self, signum: int) -> None:
        if self.signum is None:
            self.signum = signum


class SignalAdapter:
    """Injectable physical delivery/masking seam, not a POSIX implementation.

    commit_region simulates delivery exclusion. A real adapter must establish
    equivalent native ordering; lack of coroutine suspension is insufficient.
    Neither NativeSignalLatch.capture nor a real handler mutates loop objects.
    """

    def __init__(self, wake: asyncio.Event) -> None:
        self.latch = NativeSignalLatch()
        self.wake = wake
        self.accounted = False
        self.masked = False
        self.deferred: int | None = None

    def inject(self, signum: int) -> None:
        if self.masked:
            if self.deferred is None:
                self.deferred = signum
        else:
            self.latch.capture(signum)
            self.loop_notice()

    def loop_notice(self) -> None:
        self.wake.set()

    def take(self) -> Fact | None:
        if self.latch.signum is None or self.accounted:
            return None
        self.accounted = True
        return Fact(Kind.SIGNAL)

    @contextmanager
    def commit_region(self) -> Iterator[None]:
        assert not self.masked
        self.masked = True
        try:
            yield
        finally:
            self.masked = False
            if self.deferred is not None:
                self.latch.capture(self.deferred)
                self.deferred = None
                self.loop_notice()
