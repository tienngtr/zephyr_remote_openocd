# SPDX-License-Identifier: Apache-2.0

"""Local lifecycle authority shared by the helper reader and launch entry."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .cleanup import _add_failure_note
from .launch import LaunchGate
from .model import Service
from .outcome import CompletionPolicy, Diagnostic, TerminalSnapshot


class SessionError(RuntimeError):
    pass


class SessionClosedError(SessionError):
    pass


def _render_diagnostic(detail: Diagnostic) -> str:
    return "; ".join(
        (
            f"{detail.code}: {detail.message}",
            *(_render_diagnostic(child) for child in detail.diagnostics),
        )
    )


@dataclass(frozen=True, slots=True)
class _SessionSnapshot:
    ending: TerminalSnapshot | None
    reader_failure: BaseException | None
    closing: bool


class _SessionObservations:
    """Serialize recorded fatal facts, cancellation, and actual launch entry.

    Entry commits under the same lock used by the event reader. The caller then
    invokes its synchronous launch without a scheduling boundary. The lock is
    released before blocking client work so reverse output remains observable.
    """

    def __init__(self) -> None:
        self._ending: TerminalSnapshot | None = None
        self._reader_failure: BaseException | None = None
        self._primary_failure: Literal["terminal", "reader"] | None = None
        self._closing = False
        self._error_reported = False
        self._policy = CompletionPolicy.LIVE_SERVER
        self._gate = LaunchGate()
        self._changed = threading.Condition(threading.RLock())

    def snapshot(self) -> _SessionSnapshot:
        with self._changed:
            return _SessionSnapshot(self._ending, self._reader_failure, self._closing)

    def set_policy(self, policy: CompletionPolicy) -> None:
        with self._changed:
            self._policy = policy

    def record_ready(self) -> None:
        with self._changed:
            self._gate.observe_remote_ready()
            self._changed.notify_all()

    def record_terminal(self, snapshot: TerminalSnapshot) -> None:
        with self._changed:
            self._ending = snapshot
            if snapshot.operation_failed(self._policy):
                if self._primary_failure is None:
                    self._primary_failure = "terminal"
                self._error_reported = False
            self._gate.end()
            self._changed.notify_all()

    def cancel(self) -> None:
        with self._changed:
            self._closing = True
            self._gate.cancel()
            self._changed.notify_all()

    def record_reader_failure(self, error: BaseException) -> None:
        with self._changed:
            if self._reader_failure is None:
                self._reader_failure = error
                if self._primary_failure is None:
                    self._primary_failure = "reader"
                self._error_reported = False
            self._gate.fail(Diagnostic.from_exception("READER_FAILURE", error))
            self._changed.notify_all()

    def _terminal_error(self, *, include_child_status: bool = True) -> SessionError | None:
        ending = self._ending
        if ending is None or not ending.operation_failed(self._policy):
            return None
        outcome = ending.outcome
        result = outcome.child_result
        if (
            not include_child_status
            and ending.cleanup.confirmed
            and outcome.primary_failure is None
            and result is not None
            and not result.termination_requested
        ):
            return None
        details = tuple(
            _render_diagnostic(item)
            for item in (
                *((outcome.primary_failure,) if outcome.primary_failure is not None else ()),
                *outcome.diagnostics,
            )
        )
        result = outcome.child_result
        message = "; ".join(details) or (
            f"remote process failed ({result.returncode})"
            if result is not None
            else "remote session ended without an operation result"
        )
        return SessionError(message)

    def _operation_error(self, *, include_child_status: bool = True) -> SessionError | None:
        # A child status can be returned directly until stream corruption needs
        # an exception. Once it does, an earlier failing status stays primary.
        terminal = self._terminal_error(
            include_child_status=include_child_status or self._reader_failure is not None
        )
        reader = None
        if self._reader_failure is not None:
            reader = SessionError(
                "helper event stream failed: "
                + _render_diagnostic(
                    Diagnostic.from_exception("READER_FAILURE", self._reader_failure)
                )
            )
            reader.__cause__ = self._reader_failure
        primary, secondary = (
            (terminal, reader) if self._primary_failure == "terminal" else (reader, terminal)
        )
        if primary is None:
            return secondary
        if secondary is not None:
            _add_failure_note(primary, "later session failure", secondary)
        return primary

    def helper_error_for_operation(self) -> SessionError | None:
        with self._changed:
            error = self._operation_error(include_child_status=False)
            if error is not None:
                self._error_reported = True
            return error

    def take_unreported_helper_error(self) -> SessionError | None:
        with self._changed:
            # Without a terminal, close must still compose disposal uncertainty
            # behind the established reader failure instead of replacing it.
            if self._error_reported and self._ending is not None:
                return None
            error = self._operation_error()
            self._error_reported = error is not None
            return error

    def enter(self, required: Iterable[Service], forwarded: Iterable[Service]) -> None:
        with self._changed:
            error = self._operation_error()
            if error is not None:
                self._error_reported = True
                raise error
            if self._closing or self._ending is not None:
                raise SessionClosedError("operation no longer permits dependent launch")
            generation = self._gate.prepare(required)
            self._gate.observe_forwarded(forwarded)
            if not self._gate.enter(generation):
                raise SessionError("dependent launch requires READY and required forwarding")

    def wait_for_change(self, timeout: float | None) -> None:
        with self._changed:
            if self._ending is None and self._reader_failure is None and not self._closing:
                self._changed.wait(timeout)
