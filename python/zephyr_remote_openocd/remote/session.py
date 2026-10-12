# SPDX-License-Identifier: Apache-2.0

"""Local lifecycle authority shared by the helper reader and dependent work."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass

from zephyr_remote_openocd.protocol.outcome import CompletionPolicy, Diagnostic, TerminalSnapshot

from .cleanup import _add_failure_note
from .launch import LaunchGate
from .model import Service


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
class _FailureRecord:
    error: BaseException
    diagnostic: Diagnostic


@dataclass(frozen=True, slots=True)
class _SessionSnapshot:
    ending: TerminalSnapshot | None
    closing: bool
    failure: BaseException | None
    diagnostics: tuple[Diagnostic, ...]


class _SessionObservations:
    """One authority for failure recording, publication, and dependent entry.

    Physical owners report facts under this lock. The first fatal observation
    remains primary across status checks, shutdown, and finalization. Delivery
    acknowledgement never removes a recorded failure or its diagnostics.
    """

    def __init__(self) -> None:
        self._ending: TerminalSnapshot | None = None
        self._failures: dict[object, _FailureRecord] = {}
        self._closing = False
        self._error_reported = False
        self._policy = CompletionPolicy.LIVE_SERVER
        self._gate = LaunchGate()
        self._changed = threading.Condition(threading.RLock())

    def _failure(self) -> BaseException | None:
        return next((record.error for record in self._failures.values()), None)

    def snapshot(self) -> _SessionSnapshot:
        with self._changed:
            return _SessionSnapshot(
                self._ending,
                self._closing,
                self._failure(),
                tuple(record.diagnostic for record in self._failures.values()),
            )

    def set_policy(self, policy: CompletionPolicy) -> None:
        with self._changed:
            self._policy = policy

    def record_ready(self) -> None:
        with self._changed:
            self._gate.observe_remote_ready()
            self._changed.notify_all()

    def record_failure(self, code: str, error: BaseException, *, key: object | None = None) -> None:
        """Record a fatal fact once, retaining the original exception and detail."""
        with self._changed:
            identity = (code, id(error)) if key is None else key
            if identity in self._failures or any(
                record.error is error for record in self._failures.values()
            ):
                return
            diagnostic = Diagnostic.from_exception(code, error)
            primary = self._failure()
            self._failures[identity] = _FailureRecord(error, diagnostic)
            if primary is not None:
                _add_failure_note(primary, "later session failure", error)
            self._error_reported = False
            self._gate.fail(diagnostic)
            self._changed.notify_all()

    def record_terminal(self, snapshot: TerminalSnapshot) -> None:
        with self._changed:
            self._ending = snapshot
            if snapshot.operation_failed(self._policy):
                outcome = snapshot.outcome
                details = tuple(
                    _render_diagnostic(item)
                    for item in (
                        *(
                            (outcome.primary_failure,)
                            if outcome.primary_failure is not None
                            else ()
                        ),
                        *outcome.diagnostics,
                    )
                )
                result = outcome.child_result
                message = "; ".join(details) or (
                    f"remote OpenOCD failed with exit status {result.returncode}"
                    if result is not None
                    else "remote session ended without an operation result"
                )
                error = SessionError(message)
                if details and result is not None:
                    error.add_note(f"remote OpenOCD also exited with status {result.returncode}")
                self.record_failure("TERMINAL_FAILURE", error, key="terminal")
            self._gate.end()
            self._changed.notify_all()

    def cancel(self) -> None:
        with self._changed:
            self._closing = True
            self._gate.cancel()
            self._changed.notify_all()

    def record_reader_failure(self, error: BaseException) -> None:
        reader = SessionError(
            "helper event stream failed: "
            + _render_diagnostic(Diagnostic.from_exception("READER_FAILURE", error))
        )
        reader.__cause__ = error
        self.record_failure("READER_FAILURE", reader, key="reader")

    def record_control_status(self, status: int | None) -> None:
        if status not in (None, 0):
            self.record_failure(
                "CONTROL_PROCESS_FAILURE",
                SessionError(f"remote helper/SSH exited with status {status}"),
                key="control_status",
            )

    def publish_failure(self) -> BaseException | None:
        """Return the primary failure and acknowledge its current delivery."""
        with self._changed:
            error = self._failure()
            if error is not None:
                self._error_reported = True
            return error

    def operation_status(self) -> int | None:
        """Project one coherent result; failed child status is never filtered out."""
        with self._changed:
            error = self.publish_failure()
            if error is not None:
                raise error
            result = None if self._ending is None else self._ending.outcome.child_result
            return None if result is None else result.returncode

    def arbitrate_failure(self, error: BaseException) -> BaseException:
        """Record a failed effect and publish the authority's selected failure."""
        with self._changed:
            self.record_failure("OPERATION_FAILURE", error)
            return self.publish_failure() or error

    def arbitrate_advisory(self, error: BaseException) -> BaseException | None:
        """Retain advisory detail behind an established fatal outcome, if any."""
        with self._changed:
            return None if self._failure() is None else self.arbitrate_failure(error)

    def take_unreported_failure(self) -> BaseException | None:
        with self._changed:
            return None if self._error_reported else self.publish_failure()

    def enter(self, required: Iterable[Service], forwarded: Iterable[Service]) -> None:
        with self._changed:
            error = self.publish_failure()
            if error is not None:
                raise error
            if self._closing or self._ending is not None:
                raise SessionClosedError("operation no longer permits dependent launch")
            ticket = self._gate.prepare(required)
            self._gate.observe_forwarded(forwarded)
            if not self._gate.enter(ticket):
                raise SessionError("dependent launch requires READY and required forwarding")

    def wait_for_change(self, timeout: float | None) -> None:
        with self._changed:
            if self._ending is None and not self._failures and not self._closing:
                self._changed.wait(timeout)
