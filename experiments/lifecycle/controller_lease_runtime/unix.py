# SPDX-License-Identifier: Apache-2.0
"""Owned Unix boundaries: byte writer, deadlines, signals and process groups."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import fcntl
import json
import os
import signal
import subprocess
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .model import Cleaned

from .model import Diagnostic


class Timers(Protocol):
    def arm(self, name: str, seconds: float) -> asyncio.Future[None]: ...


class RealTimers:
    def arm(self, name: str, seconds: float) -> asyncio.Future[None]:
        del name
        loop = asyncio.get_running_loop()
        result: asyncio.Future[None] = loop.create_future()
        handle = loop.call_later(
            seconds, lambda: result.set_result(None) if not result.done() else None
        )
        result.add_done_callback(lambda _: handle.cancel())
        return result


class ManualTimers:
    """Clock seam: expiry runs the real determination/wait paths, not a failure fact."""

    def __init__(self) -> None:
        self.armed: dict[str, asyncio.Future[None]] = {}
        self.changed = asyncio.Condition()

    def arm(self, name: str, seconds: float) -> asyncio.Future[None]:
        del seconds
        result: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.armed[name] = result
        asyncio.create_task(self._notify())
        return result

    async def _notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    async def wait_armed(self, name: str) -> None:
        async with self.changed:
            await self.changed.wait_for(lambda: name in self.armed)

    def expire(self, name: str) -> None:
        future = self.armed[name]
        if not future.done():
            future.set_result(None)


async def readable(descriptor: int) -> None:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[None] = loop.create_future()

    def observed() -> None:
        if not future.done():
            future.set_result(None)

    loop.add_reader(descriptor, observed)
    try:
        await future
    finally:
        loop.remove_reader(descriptor)


async def bounded[T](response: asyncio.Future[T], timer: asyncio.Future[None]) -> bool:
    done, _ = await asyncio.wait((response, timer), return_when=asyncio.FIRST_COMPLETED)
    if response in done:
        timer.cancel()
        return True
    return False


class ByteWriter:
    """Bounded immutable FIFO admission; OS delivery never drives lifecycle decisions."""

    def __init__(self, descriptor: int, *, capacity: int = 262144) -> None:
        self.descriptor = descriptor
        self.capacity = capacity
        self.frames: deque[bytes] = deque()
        self.pending = 0
        self.offset = 0
        self.failure: Diagnostic | None = None
        self.on_failure: Callable[[Diagnostic], None] = lambda _: None
        self.on_progress: Callable[[], None] = lambda: None
        self.empty = asyncio.Event()
        self.empty.set()
        self.partial_writes = 0
        self.write_calls = 0
        self.registered = False
        self.was_blocking = os.get_blocking(descriptor)
        os.set_blocking(descriptor, False)

    def encode(self, event: dict[str, object]) -> bytes:
        # ASCII JSON also preserves native-path surrogateescape strings without
        # allowing UTF-8 serialization failure to interrupt ownership cleanup.
        return (json.dumps(event, separators=(',', ':')) + '\n').encode()

    def admit(self, event: dict[str, object]) -> bool:
        payload = self.encode(event)
        if self.failure is not None or self.pending + len(payload) > self.capacity:
            return False
        self.frames.append(payload)
        self.pending += len(payload)
        self.empty.clear()
        if not self.registered:
            asyncio.get_running_loop().add_writer(self.descriptor, self.flush)
            self.registered = True
        return True

    def flush(self) -> None:
        try:
            # Finite turn budget prevents a fast stdout peer starving EOF/signal handling.
            budget = 65536
            while self.frames and budget > 0:
                try:
                    count = os.write(self.descriptor, memoryview(self.frames[0])[self.offset :])
                except BlockingIOError:
                    return
                self.write_calls += 1
                self.partial_writes += count < len(self.frames[0]) - self.offset
                self.offset += count
                self.pending -= count
                budget -= count
                if self.offset == len(self.frames[0]):
                    self.frames.popleft()
                    self.offset = 0
                self.on_progress()
            if not self.frames:
                self._unregister()
                self.empty.set()
        except OSError as error:
            self.failure = Diagnostic('writer', str(error))
            self.frames.clear()
            self.pending = 0
            self._unregister()
            self.empty.set()
            self.on_failure(self.failure)

    def _unregister(self) -> None:
        if self.registered:
            asyncio.get_running_loop().remove_writer(self.descriptor)
            self.registered = False

    async def admit_terminal(self, event: dict[str, object], budget: asyncio.Future[None]) -> bool:
        if len(self.encode(event)) > self.capacity or self.failure is not None:
            return False
        if self.admit(event):
            return True
        response = asyncio.create_task(self.empty.wait())
        try:
            done, _ = await asyncio.wait((response, budget), return_when=asyncio.FIRST_COMPLETED)
            return response in done and self.admit(event)
        finally:
            response.cancel()
            await asyncio.gather(response, return_exceptions=True)

    async def drain(
        self, timers: Timers, budget: asyncio.Future[None] | None = None
    ) -> Diagnostic | None:
        if self.failure is not None:
            return self.failure
        response = asyncio.create_task(self.empty.wait())
        try:
            if not await bounded(
                response, budget if budget is not None else timers.arm('output-drain', 10)
            ):
                return Diagnostic('writer', 'final output drain deadline')
            return self.failure
        finally:
            response.cancel()
            await asyncio.gather(response, return_exceptions=True)

    def close(self) -> None:
        self._unregister()
        os.set_blocking(self.descriptor, self.was_blocking)


class SignalCapture:
    """Native handler latches only; CPython writes the wakeup pipe separately."""

    def __init__(self, report: Callable[[int], None]) -> None:
        self.report = report
        self.pending: int | None = None
        self.read_fd, self.write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.previous: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}
        self.old_wakeup: int | None = None

    def capture(self, signum: int, _frame: FrameType | None) -> None:
        if self.pending is None:
            self.pending = signum

    def install(self) -> None:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                self.previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self.capture)
            self.old_wakeup = signal.set_wakeup_fd(self.write_fd)
            asyncio.get_running_loop().add_reader(self.read_fd, self.observe)
        except BaseException:
            self.close()
            raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)

    def observe(self) -> None:
        with contextlib.suppress(BlockingIOError):
            os.read(self.read_fd, 4096)
        if self.pending is not None:
            signum, self.pending = self.pending, None
            self.report(signum)

    def close(self) -> None:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            asyncio.get_running_loop().remove_reader(self.read_fd)
            if self.old_wakeup is not None:
                signal.set_wakeup_fd(self.old_wakeup)
            for signum, handler in self.previous.items():
                signal.signal(signum, handler)
            os.close(self.read_fd)
            os.close(self.write_fd)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)


class Subreaper:
    """Scoped Linux test-host obligation: reap controlled orphan descendants.

    Not needed for the protocol. Dedicated helper entry opts in; tests restore
    the process setting and only reap descendants of owned groups.
    """

    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        if self.libc.prctl(37, ctypes.byref(old), 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'get subreaper')
        self.old = old.value
        if self.libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'set subreaper')

    def close(self) -> None:
        if self.libc.prctl(36, self.old, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'restore subreaper')


class ProcessScope:
    """Producer owns the Popen result from assignment through adoption/disposal."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.pidfd = os.pidfd_open(process.pid)
        self.closed = False
        self.descriptors_closed = False
        self.escalated = False

    def poll(self) -> int | None:
        if self.process.returncode is not None:
            return self.process.returncode
        result = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None:
            return None
        return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status

    def members(self) -> tuple[int, ...]:
        members = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit() or int(entry.name) == self.process.pid:
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) == self.process.pid:
                    members.append(int(entry.name))
            except (OSError, IndexError, ValueError):
                continue
        return tuple(members)

    async def cleanup(self, timers: Timers, generation: int) -> tuple[Diagnostic, ...]:
        failures: list[Diagnostic] = []
        # Keep leader unreaped until all group signalling finishes: PID cannot be reused.
        response = asyncio.create_task(readable(self.pidfd))
        try:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as error:
                failures.append(Diagnostic('cleanup', str(error)))
            if not await bounded(response, timers.arm(f'term-{generation}', 5)):
                self.escalated = True
            try:
                members = self.members()
            except OSError:
                members = ()  # optional inspection cannot prevent group signalling
            descriptors: list[tuple[int, int]] = []
            for pid in members:
                with contextlib.suppress(ProcessLookupError):
                    descriptors.append((pid, os.pidfd_open(pid)))
            try:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                if not response.done() and not await bounded(
                    response, timers.arm(f'reap-{generation}', 5)
                ):
                    return (Diagnostic('cleanup', 'leader did not become reapable'),)
                self.process.wait()
                for pid, descriptor in descriptors:
                    member = asyncio.create_task(readable(descriptor))
                    try:
                        if not await bounded(member, timers.arm(f'descendant-{pid}', 5)):
                            failures.append(Diagnostic('cleanup', 'descendant did not exit'))
                        else:
                            # Ordinary host init may own this non-child's reaping.
                            with contextlib.suppress(ChildProcessError):
                                os.waitid(os.P_PIDFD, descriptor, os.WEXITED)
                    finally:
                        member.cancel()
                        await asyncio.gather(member, return_exceptions=True)
                try:
                    os.killpg(self.process.pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    failures.append(Diagnostic('cleanup', 'group disappearance not confirmed'))
                self.closed = not failures
                return tuple(failures)
            finally:
                for _, descriptor in descriptors:
                    os.close(descriptor)
        except Exception as error:
            return (Diagnostic('cleanup', str(error)),)
        finally:
            response.cancel()
            await asyncio.gather(response, return_exceptions=True)

    def close_descriptors(self) -> tuple[Diagnostic, ...]:
        if self.descriptors_closed:
            return ()
        self.descriptors_closed = True
        actions = [lambda: os.close(self.pidfd)]
        actions.extend(
            stream.close
            for stream in (self.process.stdout, self.process.stderr)
            if stream is not None
        )
        failures = []
        for close in actions:
            try:
                close()
            except OSError as error:
                failures.append(Diagnostic('cleanup', str(error)))
        if failures:
            self.closed = False
        return tuple(failures)


@dataclass
class Ticket:
    generation: int
    process: subprocess.Popen[bytes] | None = None
    scope: ProcessScope | None = None
    producer: asyncio.Task[None] | None = None
    cleaning: asyncio.Task[Cleaned] | None = None
    settled: bool = False


def pipe_capacity(descriptor: int) -> int:
    return fcntl.fcntl(descriptor, fcntl.F_GETPIPE_SZ)
