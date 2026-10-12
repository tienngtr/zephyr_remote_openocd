# SPDX-License-Identifier: Apache-2.0

"""Owned nonblocking descriptors and bounded helper protocol output."""

from __future__ import annotations

import asyncio
import contextvars
import io
import os
import sys
from collections import deque
from types import TracebackType

from zephyr_remote_openocd.protocol.wire import MAX_FRAME_SIZE, encode_frame

RELAY_CHUNK_SIZE = 64 * 1024
MAX_FINAL_OBSERVATION_BYTES = MAX_FRAME_SIZE
MAX_PROTOCOL_OUTPUT_BYTES = 16 * 1024 * 1024
CHILD_RELAY_JOIN_TIMEOUT = 2
_protocol_output: contextvars.ContextVar[_ProtocolOutput | None] = contextvars.ContextVar(
    "protocol_output", default=None
)


def emit(kind, **values):
    frame = encode_frame(kind, **values)
    output = _protocol_output.get()
    if output is None:
        sys.stdout.write(frame.decode("utf-8"))
        sys.stdout.flush()
    else:
        output.enqueue(frame)


def error(message, code="HELPER_ERROR"):
    diagnostic = "\n".join((str(message), *getattr(message, "__notes__", ())))
    emit("ERROR", code=code, message=diagnostic)


async def _readable(descriptor: int) -> None:
    """Wait for fd readiness without introducing a separately buffered reader."""
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def observed():
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(descriptor, observed)
    try:
        await ready
    finally:
        loop.remove_reader(descriptor)


def _acquire_nonblocking(descriptor: int) -> bool:
    """Own mode rollback until the reader/writer can adopt the descriptor."""
    previous = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
    except BaseException as failure:
        try:
            os.set_blocking(descriptor, previous)
        except BaseException as cleanup_error:
            failure.add_note(f"descriptor mode rollback also failed: {cleanup_error}")
            for note in getattr(cleanup_error, "__notes__", ()):
                failure.add_note(f"descriptor mode rollback detail: {note}")
        raise
    return previous


class _ProtocolOutput:
    """Session-owned nonblocking JSON output with bounded buffering and cleanup."""

    def __init__(self):
        self.stream = sys.stdout
        try:
            self.descriptor: int | None = self.stream.fileno()
        except (AttributeError, io.UnsupportedOperation):
            # In-memory callers have no Unix fd or external backpressure.
            self.descriptor = None
        self.frames: deque[bytes] = deque()
        self.pending_bytes = 0
        self.offset = 0
        self.available = asyncio.Event()
        self.empty = asyncio.Event()
        self.empty.set()
        self.failure: Exception | None = None
        self.was_blocking = None
        if self.descriptor is not None:
            self.was_blocking = _acquire_nonblocking(self.descriptor)

    def enqueue(self, frame: bytes) -> None:
        if self.failure is not None:
            raise self.failure
        if self.descriptor is None:
            self.stream.write(frame.decode("utf-8"))
            self.stream.flush()
            return
        if self.pending_bytes + len(frame) > MAX_PROTOCOL_OUTPUT_BYTES:
            raise BufferError("protocol output backlog exceeded its bound")
        self.frames.append(frame)
        self.pending_bytes += len(frame)
        self.empty.clear()
        self.available.set()

    async def _writable(self) -> None:
        assert self.descriptor is not None
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def observed():
            if not ready.done():
                ready.set_result(None)

        loop.add_writer(self.descriptor, observed)
        try:
            await ready
        finally:
            loop.remove_writer(self.descriptor)

    async def run(self) -> None:
        assert self.descriptor is not None
        try:
            while True:
                await self.available.wait()
                while self.frames:
                    frame = self.frames[0]
                    try:
                        count = os.write(self.descriptor, memoryview(frame)[self.offset :])
                    except BlockingIOError:
                        await self._writable()
                        continue
                    self.offset += count
                    self.pending_bytes -= count
                    if self.offset == len(frame):
                        self.frames.popleft()
                        self.offset = 0
                self.available.clear()
                self.empty.set()
        except Exception as exc:
            self.failure = exc
            self.empty.set()
            raise

    async def drain(self) -> None:
        try:
            async with asyncio.timeout(CHILD_RELAY_JOIN_TIMEOUT):
                await self.empty.wait()
        except TimeoutError as exc:
            raise RuntimeError("protocol output did not drain before cleanup deadline") from exc
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        try:
            if self.descriptor is not None and self.was_blocking is not None:
                os.set_blocking(self.descriptor, self.was_blocking)
        finally:
            self.frames.clear()
            self.pending_bytes = 0
            self.offset = 0
            self.empty.set()


class _AsyncInput:
    """One cancellable raw observation path for an owned input descriptor."""

    def __init__(self, descriptor: int):
        self.descriptor = descriptor
        self._wake: asyncio.Future[None] | None = None
        self._checkpoint: asyncio.Future[None] | None = None
        self._scan_remaining = 0
        self._closed = False
        self.was_blocking = _acquire_nonblocking(descriptor)

    def __enter__(self) -> _AsyncInput:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        failure: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:
            # Cancellation is observer settlement, not an established failure.
            # Its cleanup failure must still reach the coordinator's guard.
            if failure is None or isinstance(failure, asyncio.CancelledError):
                raise
            failure.add_note(f"input descriptor restoration also failed: {cleanup_error}")
            for note in getattr(cleanup_error, "__notes__", ()):
                failure.add_note(f"input descriptor restoration detail: {note}")

    def _observed(self) -> None:
        if self._checkpoint is not None and not self._checkpoint.done():
            self._scan_remaining = 0
            self._checkpoint.set_result(None)

    def _wake_reader(self) -> None:
        if self._wake is not None and not self._wake.done():
            self._wake.set_result(None)

    def checkpoint(self) -> asyncio.Future[None]:
        """Request a finite available-byte scan by the sole descriptor reader."""
        self._checkpoint = asyncio.get_running_loop().create_future()
        self._scan_remaining = MAX_FINAL_OBSERVATION_BYTES
        if self._closed:
            self._observed()
        else:
            self._wake_reader()
        return self._checkpoint

    async def read(self) -> bytes:
        while True:
            try:
                size = min(RELAY_CHUNK_SIZE, self._scan_remaining or RELAY_CHUNK_SIZE)
                chunk = os.read(self.descriptor, size)
            except BlockingIOError:
                self._observed()
                loop = asyncio.get_running_loop()
                self._wake = loop.create_future()
                loop.add_reader(self.descriptor, self._wake_reader)
                try:
                    await self._wake
                finally:
                    loop.remove_reader(self.descriptor)
                    self._wake = None
            else:
                if not chunk:
                    self._observed()
                elif self._scan_remaining:
                    self._scan_remaining -= len(chunk)
                    if not self._scan_remaining:
                        self._observed()
                return chunk

    def close(self) -> None:
        self._closed = True
        self._observed()
        os.set_blocking(self.descriptor, self.was_blocking)
