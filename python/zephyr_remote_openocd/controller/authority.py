# SPDX-License-Identifier: Apache-2.0

"""Local lifecycle authority shared by the helper reader and dependent work."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from zephyr_remote_openocd.controller.admission import AdmissionGate
from zephyr_remote_openocd.controller.cleanup import _add_failure_note
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.plan import SessionAllocation
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy, Diagnostic, TerminalSnapshot


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
class SessionSnapshot:
    terminal: TerminalSnapshot | None
    closing: bool
    failure: BaseException | None
    diagnostics: tuple[Diagnostic, ...]
    allocation: SessionAllocation | None = None
    ready_address: str | None = None


def _no_notification() -> None:
    pass


class SessionAuthority:
    """One authority for failure recording, publication, and dependent entry.

    Physical owners report facts under this lock. The first fatal observation
    remains primary across status checks, shutdown, and finalization. Delivery
    acknowledgement never removes a recorded failure or its diagnostics.
    Each state change is published before ``notify`` wakes the coordinator,
    which re-reads this authority rather than interpreting the wakeup.
    """

    def __init__(self, notify: Callable[[], None] = _no_notification) -> None:
        self._terminal: TerminalSnapshot | None = None
        self._failures: dict[object, _FailureRecord] = {}
        self._closing = False
        self._error_reported = False
        self._policy = CompletionPolicy.LIVE_SERVER
        self._gate = AdmissionGate()
        self._allocation: SessionAllocation | None = None
        self._ready_address: str | None = None
        self._changed = threading.RLock()
        self._notify = notify

    def _failure(self) -> BaseException | None:
        return next((record.error for record in self._failures.values()), None)

    def snapshot(self) -> SessionSnapshot:
        with self._changed:
            return SessionSnapshot(
                self._terminal,
                self._closing,
                self._failure(),
                tuple(record.diagnostic for record in self._failures.values()),
                self._allocation,
                self._ready_address,
            )

    def set_policy(self, policy: CompletionPolicy) -> None:
        with self._changed:
            self._policy = policy

    def record_created(self, allocation: SessionAllocation) -> None:
        with self._changed:
            self._allocation = allocation
            self._notify()

    def record_ready(self, address: str | None = None) -> None:
        with self._changed:
            if address is not None:
                self._ready_address = address
            self._gate.observe_remote_ready()
            self._notify()

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
            self._notify()

    def record_terminal(self, snapshot: TerminalSnapshot) -> None:
        with self._changed:
            self._terminal = snapshot
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
            self._notify()

    def cancel(self) -> None:
        with self._changed:
            self._closing = True
            self._gate.cancel()
            self._notify()

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
            result = None if self._terminal is None else self._terminal.outcome.child_result
            return None if result is None else result.returncode

    def resolve_effect_failure(self, error: BaseException) -> BaseException:
        """Record a failed effect and publish the authority's selected failure."""
        with self._changed:
            self.record_failure("OPERATION_FAILURE", error)
            return self.publish_failure() or error

    def resolve_optional_failure(self, error: BaseException) -> BaseException | None:
        """Retain advisory detail behind an established fatal outcome, if any."""
        with self._changed:
            return None if self._failure() is None else self.resolve_effect_failure(error)

    def take_unreported_failure(self) -> BaseException | None:
        with self._changed:
            return None if self._error_reported else self.publish_failure()

    def admit_work(self, required: Iterable[ForwardSpec], forwarded: Iterable[ForwardSpec]) -> None:
        with self._changed:
            error = self.publish_failure()
            if error is not None:
                raise error
            if self._closing or self._terminal is not None:
                raise SessionClosedError("operation no longer permits dependent launch")
            ticket = self._gate.prepare(required)
            self._gate.observe_forwarded(forwarded)
            if not self._gate.enter(ticket):
                raise SessionError("dependent launch requires READY and required forwarding")
