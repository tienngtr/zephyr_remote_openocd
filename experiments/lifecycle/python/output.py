# SPDX-License-Identifier: Apache-2.0
"""Bounded local output admission and independently scheduled physical writing."""

from __future__ import annotations

import asyncio
import json

from .contracts import Diagnostic, EffectFailure, Frame, Sink, diagnostic_from


def frame(kind: str, generation: int = 0, **payload: str | int | list[str] | None) -> Frame:
    encoded = json.dumps({'version': 1, 'type': kind, **payload}).encode() + b'\n'
    return Frame(kind, generation, encoded)


class ProtocolWriter:
    """A retained original failure result, plus bounded FIFO/in-flight credit."""

    def __init__(
        self, sink: Sink, wake: asyncio.Event, *, capacity: int = 8, byte_limit: int = 4096
    ) -> None:
        if capacity < 1 or byte_limit < 1:
            raise ValueError('protocol admission must be bounded')
        self.sink = sink
        self.wake = wake
        self.capacity = capacity
        self.byte_limit = byte_limit
        self.queue: asyncio.Queue[Frame] = asyncio.Queue(capacity)
        self.available = asyncio.Event()
        self.outstanding = 0
        self.pending_bytes = 0
        self.failure: Diagnostic | None = None
        self.finishing = False
        self.current: Frame | None = None
        self.offset = 0
        self.delivered: list[Frame] = []  # experiment trace, not a production buffer

    def admit(self, event: Frame) -> None:
        if self.failure is not None:
            raise EffectFailure(self.failure)
        if len(event.encoded) > self.byte_limit:
            raise EffectFailure(Diagnostic('output-oversize', 'frame exceeds local byte bound'))
        if (
            self.outstanding >= self.capacity
            or self.pending_bytes + len(event.encoded) > self.byte_limit
        ):
            raise EffectFailure(Diagnostic('output-full', 'local protocol buffer full'))
        self.queue.put_nowait(event)
        self.outstanding += 1
        self.pending_bytes += len(event.encoded)
        self.available.set()

    def finish(self) -> None:
        self.finishing = True
        self.available.set()

    async def run(self) -> None:
        try:
            while True:
                if self.queue.empty():
                    if self.finishing:
                        return
                    self.available.clear()
                    await self.available.wait()
                    continue
                self.current = self.queue.get_nowait()
                self.offset = 0
                while self.offset < len(self.current.encoded):
                    count = await self.sink.write(self.current.encoded[self.offset :])
                    if not 0 < count <= len(self.current.encoded) - self.offset:
                        raise EffectFailure(Diagnostic('writer', 'invalid write completion'))
                    self.offset += count
                    self.pending_bytes -= count
                    self.wake.set()
                self.delivered.append(self.current)
                self.current = None
                self.outstanding -= 1
                self.wake.set()
        except BaseException as exception:
            self.failure = diagnostic_from(exception)
            # Delivery is abandoned; the immutable committed events stay local.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.wake.set()
