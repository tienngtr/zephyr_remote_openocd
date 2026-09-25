# SPDX-License-Identifier: Apache-2.0

"""Remote-session errors and synchronized helper observations."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


class SessionError(RuntimeError):
    pass


class SessionClosedError(SessionError):
    pass


@dataclass(frozen=True, slots=True)
class _SessionClosed:
    reason: Literal["requested", "process_exit"]
    returncode: int | None


@dataclass(frozen=True, slots=True)
class _HelperError:
    error: SessionError


_SessionEnding = _SessionClosed | _HelperError


@dataclass(frozen=True, slots=True)
class _SessionSnapshot:
    ending: _SessionEnding | None
    reader_failure: BaseException | None
    stop_requested: bool


@dataclass(frozen=True, slots=True)
class _StopWritten:
    pass


_StopResult = _StopWritten | _SessionEnding


class _SessionObservations:
    """Synchronize facts observed from the helper event stream."""

    def __init__(self) -> None:
        self._ending: _SessionEnding | None = None
        self._reader_failure: BaseException | None = None
        self._stop_requested = False
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def snapshot(self) -> _SessionSnapshot:
        with self._changed:
            return _SessionSnapshot(
                self._ending,
                self._reader_failure,
                self._stop_requested,
            )

    def record_close(
        self,
        reason: Literal["requested", "process_exit"],
        returncode: int | None,
    ) -> None:
        """Record a session-close event and wake result waiters."""
        with self._changed:
            self._ending = _SessionClosed(reason, returncode)
            self._changed.notify_all()

    def record_error_event(self, error: SessionError) -> None:
        """Record the helper's ERROR event and wake result waiters."""
        with self._changed:
            self._ending = _HelperError(error)
            self._changed.notify_all()

    def record_reader_failure(self, error: BaseException) -> None:
        """Record an event-reader failure and wake result waiters."""
        with self._changed:
            self._reader_failure = error
            self._changed.notify_all()

    def wait_for_change(self, timeout: float | None) -> None:
        with self._changed:
            if not self._has_result_locked():
                self._changed.wait(timeout)

    def request_stop(self, write_stop: Callable[[], None]) -> _StopResult:
        """Write STOP atomically with session-ending event observation.

        The write occurs while holding the state lock so a session-ending event
        cannot be mistaken for an unsolicited shutdown between the write and
        recording that STOP was requested.
        """
        with self._changed:
            if self._ending is not None:
                return self._ending
            write_stop()
            self._stop_requested = True
            return _StopWritten()

    def _has_result_locked(self) -> bool:
        if self._reader_failure is not None or isinstance(self._ending, _HelperError):
            return True
        return isinstance(self._ending, _SessionClosed) and (
            self._ending.reason == "process_exit"
            or (self._ending.reason == "requested" and not self._stop_requested)
        )
