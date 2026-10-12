# SPDX-License-Identifier: Apache-2.0

"""Durable native Ctrl-C observation and synchronous cancellation dispatch."""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

from zephyr_remote_openocd.controller.cleanup import _add_failure_note

_OBSERVATION_INTERVAL = 0.1
_CURRENT = threading.local()


class LocalCancellation:
    """Own the native signal fact, without selecting a session outcome."""

    def __init__(self) -> None:
        self.interrupted = False
        self.error = KeyboardInterrupt()
        self._deferred = 0
        self._previous_handler = signal.getsignal(signal.SIGINT)

    def observe(self, _signum: int, _frame: FrameType | None) -> None:
        # A raising handler can run inside a GC weakref callback, where CPython
        # reports its exception as unraisable and consumes it. Only latch here:
        # normal checkpoints carry cancellation into rollback and the authority.
        self.interrupted = True

    def pending(self) -> KeyboardInterrupt | None:
        if not self.interrupted or self._deferred:
            return None
        if signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, set()):
            return None
        return self.error

    @contextmanager
    def defer(self) -> Iterator[None]:
        """Keep cleanup attempts independent of cancellation dispatch."""
        self._deferred += 1
        try:
            yield
        finally:
            self._deferred -= 1

    @contextmanager
    def interactive_client(self) -> Iterator[None]:
        """Let the stock GDB client own Ctrl-C without latching its signals."""
        checkpoint()
        try:
            signal.signal(signal.SIGINT, self._previous_handler)
            checkpoint()
            _CURRENT.scope = None
            yield
        finally:
            _CURRENT.scope = self
            signal.signal(signal.SIGINT, self.observe)


def current_cancellation() -> LocalCancellation | None:
    """Return only the calling thread's operation scope."""
    return getattr(_CURRENT, "scope", None)


def pending_cancellation() -> KeyboardInterrupt | None:
    scope = current_cancellation()
    return None if scope is None else scope.pending()


def checkpoint() -> None:
    """Dispatch a durable interruption outside native signal callbacks."""
    error = pending_cancellation()
    if error is not None:
        raise error


def observation_timeout(timeout: float | None) -> float | None:
    """Bound operation waits only when durable cancellation is active."""
    scope = current_cancellation()
    if scope is None or scope._deferred:
        return timeout
    return _OBSERVATION_INTERVAL if timeout is None else min(timeout, _OBSERVATION_INTERVAL)


@contextmanager
def defer_cancellation() -> Iterator[None]:
    """Settle independent resources before reporting a pending interruption."""
    scope = current_cancellation()
    if scope is None:
        yield
    else:
        with scope.defer():
            yield


@contextmanager
def interactive_client() -> Iterator[None]:
    """Preserve the caller's interactive GDB signal semantics."""
    scope = current_cancellation()
    if scope is None:
        yield
    else:
        with scope.interactive_client():
            yield


@contextmanager
def local_cancellation() -> Iterator[LocalCancellation | None]:
    """Observe main-thread Ctrl-C without stealing a concurrent caller's signals."""
    enclosing = current_cancellation()
    if enclosing is not None:
        checkpoint()
        yield enclosing
        checkpoint()
        return
    if threading.current_thread() is not threading.main_thread():
        # Python dispatches native handlers only on the main thread. Worker
        # operations retain their caller's cancellation policy and own lifecycle.
        yield None
        return
    scope = LocalCancellation()
    primary: BaseException | None = None
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        _CURRENT.scope = scope
        signal.signal(signal.SIGINT, scope.observe)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        checkpoint()
        yield scope
    except BaseException as error:
        primary = error
        raise
    finally:
        # Deliver pending native signals to the observer before restoring the
        # caller's handler. No resource acquisition depends on scope publication.
        restoration_errors: list[BaseException] = []
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except BaseException as error:
            restoration_errors.append(error)
        try:
            signal.signal(signal.SIGINT, scope._previous_handler)
        except BaseException as error:
            restoration_errors.append(error)
        finally:
            _CURRENT.scope = None
        for restoration_error in restoration_errors:
            if primary is None:
                primary = restoration_error
            else:
                _add_failure_note(primary, "signal restoration also failed", restoration_error)
        if scope.interrupted:
            if primary is None:
                raise scope.error
            if primary is not scope.error:
                _add_failure_note(primary, "local cancellation also requested", scope.error)
        if restoration_errors and primary is restoration_errors[0]:
            raise primary
