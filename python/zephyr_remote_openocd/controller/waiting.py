# SPDX-License-Identifier: Apache-2.0

"""One event-driven wait for active operation work on its owning thread.

A wait composes native cancellation wakeup, application notifications,
descriptor readiness, process completion, and an absolute deadline. Wakeups are
hints: after consuming them, the wait dispatches a recorded cancellation before
returning readiness, and callers re-snapshot their durable state.
"""

from __future__ import annotations

import errno
import math
import os
import select
import threading
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from zephyr_remote_openocd.controller.cancellation import current_operation

# Process completion uses pidfds. Without kernel pidfd support, a non-reaping
# waitid() re-check bounds how late an exit can be observed; cancellation and
# descriptor readiness remain event-driven.
_PIDFD_FALLBACK_INTERVAL = 0.05

_READ_EVENTS = select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR
_WRITE_EVENTS = select.POLLOUT | select.POLLHUP | select.POLLERR


class HasFileno(Protocol):
    def fileno(self) -> int: ...


type Descriptor = int | HasFileno


class ProcessHandle(Protocol):
    """A child whose owner alone reaps it on the waiting thread."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...


class Notifier:
    """Coalescing, nonblocking wakeup for state published by other threads.

    Producers publish durable state first and then notify. The consumer drains
    hints only after waking and before re-reading that state, so a notification
    issued after its snapshot remains pending for the next wait. The owner opens
    and closes the descriptors; late notifications after closure are ignored.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fds: tuple[int, int] | None = None
        self._closed = False

    def open(self) -> None:
        with self._lock:
            if self._fds is None and not self._closed:
                self._fds = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)

    def notify(self) -> None:
        with self._lock:
            if self._fds is None:
                return
            # A full pipe already holds a pending wakeup.
            with suppress(BlockingIOError):
                os.write(self._fds[1], b"\0")

    def fileno(self) -> int | None:
        fds = self._fds
        return None if fds is None else fds[0]

    def drain(self) -> None:
        with self._lock:
            if self._fds is None:
                return
            try:
                while os.read(self._fds[0], 4096):
                    pass
            except BlockingIOError:
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            fds, self._fds = self._fds, None
        if fds is not None:
            for descriptor in fds:
                os.close(descriptor)


@dataclass(frozen=True, slots=True)
class WaitSources:
    """State notifications and owned processes that a dependent wait observes."""

    notifiers: tuple[Notifier, ...] = ()
    processes: tuple[ProcessHandle, ...] = ()


@dataclass(frozen=True, slots=True)
class WaitResult:
    """Readiness observed by one wait; read and write readiness stay distinct."""

    readable: frozenset[int]
    writable: frozenset[int]
    exited: frozenset[int]
    notified: bool
    expired: bool


_NO_SOURCES = WaitSources()


def _descriptor(item: Descriptor) -> int:
    return item if isinstance(item, int) else item.fileno()


def _exited_unreaped(pid: int) -> bool:
    """Observe exit without reaping, so the process owner remains sole reaper."""
    try:
        result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return result is not None and result.si_pid != 0


def _timeout_ms(deadline: float | None, *, immediate: bool, fallback: bool) -> int | None:
    if immediate:
        return 0
    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    if fallback:
        remaining = (
            _PIDFD_FALLBACK_INTERVAL
            if remaining is None
            else min(remaining, _PIDFD_FALLBACK_INTERVAL)
        )
    return None if remaining is None else math.ceil(remaining * 1000)


def wait_for(
    *,
    readable: Iterable[Descriptor] = (),
    writable: Iterable[Descriptor] = (),
    sources: WaitSources = _NO_SOURCES,
    deadline: float | None = None,
) -> WaitResult:
    """Block until readiness, completion, notification, deadline, or cancellation.

    A requested cancellation is dispatched before blocking and again after
    wakeup, taking precedence over coincident readiness. Process exit and pipe
    EOF are reported independently; neither is inferred from the other.
    """
    operation = current_operation()
    if operation is not None:
        operation.dispatch()
    read_fds = frozenset(_descriptor(item) for item in readable)
    write_fds = frozenset(_descriptor(item) for item in writable)
    masks: dict[int, int] = {}
    for descriptor in read_fds:
        masks[descriptor] = masks.get(descriptor, 0) | _READ_EVENTS
    for descriptor in write_fds:
        masks[descriptor] = masks.get(descriptor, 0) | _WRITE_EVENTS
    notifier_fds = {
        notify_fd: notifier
        for notifier in sources.notifiers
        if (notify_fd := notifier.fileno()) is not None
    }
    native_fd = None if operation is None else operation.native_wakeup_fd
    exited: set[int] = set()
    fallback: list[int] = []
    pidfds: dict[int, int] = {}
    try:
        for process in sources.processes:
            if process.returncode is not None:
                exited.add(process.pid)
                continue
            try:
                pidfds[os.pidfd_open(process.pid)] = process.pid
            except OSError:
                if _exited_unreaped(process.pid):
                    exited.add(process.pid)
                else:
                    fallback.append(process.pid)
        poller = select.poll()
        for descriptor in (*notifier_fds, *pidfds, *(() if native_fd is None else (native_fd,))):
            masks[descriptor] = masks.get(descriptor, 0) | select.POLLIN
        for descriptor, mask in masks.items():
            poller.register(descriptor, mask)
        events = poller.poll(_timeout_ms(deadline, immediate=bool(exited), fallback=bool(fallback)))
        ready_read: set[int] = set()
        ready_write: set[int] = set()
        notified = False
        native_woken = False
        for descriptor, event in events:
            if event & select.POLLNVAL:
                raise OSError(errno.EBADF, "wait descriptor is not open", descriptor)
            if descriptor in read_fds and event & _READ_EVENTS:
                ready_read.add(descriptor)
            if descriptor in write_fds and event & _WRITE_EVENTS:
                ready_write.add(descriptor)
            if descriptor in pidfds and event & select.POLLIN:
                exited.add(pidfds[descriptor])
            if descriptor in notifier_fds:
                notifier_fds[descriptor].drain()
                notified = True
            if descriptor == native_fd:
                native_woken = True
        exited.update(pid for pid in fallback if _exited_unreaped(pid))
    finally:
        for pidfd in pidfds:
            os.close(pidfd)
    if operation is not None:
        if native_woken:
            operation.drain_native_wakeup()
        operation.dispatch()
    return WaitResult(
        frozenset(ready_read),
        frozenset(ready_write),
        frozenset(exited),
        notified,
        deadline is not None and time.monotonic() >= deadline,
    )
