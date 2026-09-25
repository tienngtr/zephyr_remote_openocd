# SPDX-License-Identifier: Apache-2.0

"""Remote-session errors shared by the concrete session implementation."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import cast


class SessionError(RuntimeError):
    pass


class SessionClosedError(SessionError):
    pass


class _SessionState:
    """Synchronize facts observed from the helper event stream.

    This object records event observations and whether a helper error has been
    reported. The foreground session remains responsible for forwarding
    health, cleanup order, and deciding which operation failure is primary.
    """

    def __init__(self) -> None:
        self._openocd_returncode: int | None = None
        self._reader_error: BaseException | None = None
        self._error_event: BaseException | None = None
        self._error_event_reported = False
        self._close_reason: str | None = None
        self._stop_requested = False
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    @property
    def openocd_returncode(self) -> int | None:
        with self._changed:
            return self._openocd_returncode

    @property
    def close_reason(self) -> str | None:
        with self._changed:
            return self._close_reason

    @property
    def has_error_event(self) -> bool:
        with self._changed:
            return self._error_event is not None

    @property
    def error_event(self) -> BaseException | None:
        with self._changed:
            return self._error_event

    def record_close(self, reason: str, returncode: int | None) -> None:
        """Record a session-close event and wake result waiters."""
        with self._changed:
            self._close_reason = reason
            if reason == "process_exit":
                self._openocd_returncode = int(cast(int, returncode))
            elif not self._stop_requested:
                self._reader_error = SessionError(
                    "helper reported SESSION_CLOSED(reason='requested') before STOP"
                )
            self._changed.notify_all()

    def record_error_event(self, error: BaseException, *, reported: bool) -> None:
        """Record the helper's ERROR event and wake result waiters."""
        with self._changed:
            self._error_event = error
            self._error_event_reported = reported
            self._changed.notify_all()

    def take_unreported_error_event(self) -> BaseException | None:
        """Claim a helper error for close-time reporting only once."""
        with self._changed:
            if self._error_event is None or self._error_event_reported:
                return None
            self._error_event_reported = True
            return self._error_event

    def record_reader_failure(self, error: BaseException) -> None:
        """Record an event-reader failure and wake result waiters."""
        with self._changed:
            self._reader_error = error
            self._changed.notify_all()

    def recorded_openocd_exit(self) -> int | None:
        """Return a natural result or raise the observed helper or reader error."""
        with self._changed:
            if self._reader_error is not None:
                reader_error = self._reader_error
                raise SessionError(f"helper event stream failed: {reader_error}") from reader_error
            if self._error_event is not None:
                self._error_event_reported = True
                raise self._error_event
            return self._openocd_returncode

    def reader_failure(self) -> BaseException | None:
        """Return a session-facing wrapper for a recorded reader failure."""
        with self._changed:
            if self._reader_error is None:
                return None
            failure = SessionError(f"helper event stream failed: {self._reader_error}")
            failure.__cause__ = self._reader_error
            return failure

    def reader_failure_has_eof_cause(self) -> bool:
        with self._changed:
            return isinstance(
                self._reader_error.__cause__ if self._reader_error is not None else None,
                EOFError,
            )

    def wait_for_change(self, timeout: float | None) -> None:
        with self._changed:
            if (
                self._reader_error is None
                and self._error_event is None
                and self._openocd_returncode is None
            ):
                self._changed.wait(timeout)

    def request_stop(self, write_stop: Callable[[], None]) -> str | None:
        """Write STOP atomically with session-ending event observation.

        The write occurs while holding the state lock so a session-ending event
        cannot be mistaken for an unsolicited shutdown between the write and
        recording that STOP was requested.
        """
        with self._changed:
            if self._close_reason is not None or self._error_event is not None:
                return self._close_reason
            write_stop()
            self._stop_requested = True
            return None
