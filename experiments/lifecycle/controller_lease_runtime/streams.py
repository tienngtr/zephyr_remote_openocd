# SPDX-License-Identifier: Apache-2.0
"""Single-owner pipe observation with a bounded final nonblocking prefix scan."""

from __future__ import annotations

import array
import asyncio
import codecs
import fcntl
import os
import termios
from collections import deque
from collections.abc import Callable

from .model import Diagnostic, Marker
from .unix import pipe_capacity


class Matcher:
    """Bounded candidate indices, including trimmed lines and EOF completion."""

    def __init__(self, required: frozenset[str], found: Callable[[str], None]) -> None:
        self.required = required
        self.found = found
        self.seen: set[str] = set()
        self.indices: dict[str, int] = {}
        self.started = False
        self.reset()

    def reset(self) -> None:
        self.indices = {name: 0 for name in self.required - self.seen}
        self.started = False

    def feed(self, text: str, *, final: bool) -> None:
        for char in text:
            if char == '\n':
                self.finish()
                continue
            if not self.started and char.isspace():
                continue
            self.started = True
            for name, index in tuple(self.indices.items()):
                if index < len(name) and name[index] == char:
                    self.indices[name] = index + 1
                elif index != len(name) or not char.isspace():
                    del self.indices[name]
        if final:
            self.finish()

    def finish(self) -> None:
        for name, index in self.indices.items():
            if self.started and index == len(name):
                self.seen.add(name)
                self.found(name)
        self.reset()


class Stream:
    def __init__(
        self,
        descriptor: int,
        name: str,
        generation: int,
        required: frozenset[str],
        marker: Callable[[Marker], None],
        changed: Callable[[], None],
        failed: Callable[[Diagnostic], None],
    ) -> None:
        self.descriptor, self.name, self.generation = descriptor, name, generation
        self.changed = changed
        self.failed = failed
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.matcher = Matcher(required, lambda text: marker(Marker(generation, text)))
        self.pending: deque[str] = deque()
        self.pending_bytes = 0
        self.normal_limit = 65536
        # A final prefix can consume at most this kernel pipe's capacity. Three
        # UTF-8 bytes per replacement character bound decode expansion.
        self.limit = self.normal_limit + 3 * pipe_capacity(descriptor) + 16
        self.peak = 0
        self.ended = False
        self.paused = False
        self.collision_tail = ''
        self.collision = False
        os.set_blocking(descriptor, False)
        asyncio.get_running_loop().add_reader(descriptor, self.observe)

    def pause(self) -> None:
        self.paused = True
        asyncio.get_running_loop().remove_reader(self.descriptor)

    def resume(self) -> None:
        if self.paused and not self.ended and self.pending_bytes < self.normal_limit - 12288:
            self.paused = False
            asyncio.get_running_loop().add_reader(self.descriptor, self.observe)

    def observe(self) -> None:
        if self.ended:
            return
        if self.pending_bytes >= self.normal_limit - 12288:
            self.pause()
            return
        self.read(4096)

    def read(self, count: int) -> int:
        try:
            chunk = os.read(self.descriptor, count)
        except BlockingIOError:
            return -1
        except OSError as error:
            self.stop_failed(error)
            return -1
        text = self.decoder.decode(chunk, final=not chunk)
        self.matcher.feed(text, final=not chunk)
        if text:
            self.pending.append(text)
            self.pending_bytes += len(text.encode())
            self.peak = max(self.peak, self.pending_bytes)
            assert self.pending_bytes <= self.limit
            phrase = self.collision_tail + text.casefold()
            self.collision |= 'address already in use' in phrase
            self.collision_tail = phrase[-22:]
        if not chunk:
            self.ended = True
            self.pause()
        self.changed()
        return len(chunk)

    def final_scan(self) -> None:
        if self.ended:
            return
        available = array.array('i', [0])
        try:
            fcntl.ioctl(self.descriptor, termios.FIONREAD, available, True)
        except OSError as error:
            self.stop_failed(error)
            return
        remaining = min(available[0], max(0, (self.limit - self.pending_bytes - 16) // 3))
        while remaining:
            count = self.read(min(4096, remaining))
            if count <= 0:
                break
            remaining -= count
        # Probe EOF, never wait for more bytes. A writer may race this final
        # probe; accepting that observation is policy, not a global time fence.
        self.read(1)

    def stop_failed(self, error: OSError) -> None:
        self.close()
        self.failed(Diagnostic(self.name, str(error)))
        self.changed()

    def pop(self) -> str:
        text = self.pending.popleft()
        self.pending_bytes -= len(text.encode())
        return text

    def close(self) -> None:
        self.ended = True
        self.pause()
