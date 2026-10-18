# SPDX-License-Identifier: Apache-2.0

"""Local lifecycle authority shared by the helper reader and dependent work."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from zephyr_remote_openocd.controller._cleanup import add_failure_note
from zephyr_remote_openocd.controller.admission import AdmissionGate
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.plan import SessionAllocation
from zephyr_remote_openocd.protocol.outcome import (
    CompletionPolicy,
    Diagnostic,
    TerminalSnapshot,
    Trigger,
)


class SessionError(RuntimeError):
    pass


class SessionClosedError(SessionError):
    pass


class SessionEndingError(SessionClosedError):
    """Refuse a new effect while final control observation is pending.

    The refusal does not select the operation failure: the authority records
    it only after final accounting, behind any detail that accounting finds.
    """


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
    control_ended: bool = False
    control_observation_pending: bool = False


def _no_notification() -> None:
    pass


def _admit_unconditionally() -> None:
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
        self._control_ended = False
        self._control_observation_pending = False
        self._deferred_refusals: list[tuple[str, BaseException, object]] = []
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
                self._control_ended,
                self._control_observation_pending,
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
            if self._control_observation_pending and isinstance(error, SessionEndingError):
                if all(deferred is not error for _, deferred, _ in self._deferred_refusals):
                    self._deferred_refusals.append((code, error, identity))
                return
            diagnostic = Diagnostic.from_exception(code, error)
            primary = self._failure()
            self._failures[identity] = _FailureRecord(error, diagnostic)
            if primary is not None:
                add_failure_note(primary, "later session failure", error)
            self._error_reported = False
            self._gate.fail(diagnostic)
            self._notify()

    def record_terminal(self, snapshot: TerminalSnapshot) -> None:
        with self._changed:
            self._terminal = snapshot
            if snapshot.outcome.trigger == Trigger.CONTROLLER_EOF and not self._closing:
                # Input EOF ends the controller lease only after local closure
                # begins; earlier, the remote saw the control channel fail. The
                # EOF initiated remote termination, so it precedes any terminal
                # failure recorded below.
                self.record_failure(
                    "UNEXPECTED_CONTROLLER_EOF",
                    SessionError(
                        "remote helper observed controller-input EOF before local closure"
                    ),
                    key="controller_eof",
                )
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

    def begin_control_exit(self) -> None:
        """Revoke admission before the reader completes final observation."""
        with self._changed:
            self._control_ended = True
            self._control_observation_pending = True
            self._notify()

    def finish_control_exit(self) -> None:
        """Publish final accounting without holding the lock across physical I/O.

        Refusals issued while accounting was pending are recorded last, so
        they become primary only when accounting established no failure.
        """
        with self._changed:
            if self._terminal is None:
                self.record_failure(
                    "CONTROL_ENDED",
                    SessionError("remote helper control transport ended without SESSION_ENDED"),
                    key="control_ended",
                )
            self._control_observation_pending = False
            refusals, self._deferred_refusals = self._deferred_refusals, []
            for code, error, key in refusals:
                self.record_failure(code, error, key=key)
            self._notify()

    def _refuse_pending_observation(self, message: str) -> None:
        """Refuse new effects without selecting a failure before final accounting."""
        if self._control_observation_pending:
            raise SessionEndingError(message)

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
            if self._control_observation_pending:
                return None
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

    def admit_work(
        self,
        required: Iterable[ForwardSpec],
        forwarded: Iterable[ForwardSpec],
        *,
        final_admission: Callable[[], None] = _admit_unconditionally,
    ) -> None:
        """Commit dependent-work entry at one linearization point.

        Session facts are recorded only under this lock, so they cannot change
        while it is held. ``final_admission`` is the caller's last refusal
        condition; it runs after every session condition and immediately before
        the gate commits entry. A condition it reads after it returns does not
        revoke this admission.
        """
        with self._changed:
            error = self.publish_failure()
            if error is not None:
                raise error
            if self._closing:
                raise SessionClosedError("operation no longer permits dependent launch")
            self._refuse_pending_observation("operation no longer permits dependent launch")
            if self._terminal is not None or self._control_ended:
                raise SessionClosedError("operation no longer permits dependent launch")
            ticket = self._gate.prepare(required)
            self._gate.observe_forwarded(forwarded)
            final_admission()
            if not self._gate.enter(ticket):
                raise SessionError("dependent launch requires READY and required forwarding")

    def admit_process_start(self, final_admission: Callable[[], None]) -> None:
        """Commit START after accounting facts the helper reader already recorded."""
        with self._changed:
            error = self.publish_failure()
            if error is not None:
                raise error
            self._refuse_pending_observation("helper session ended before START")
            if self._terminal is not None or self._control_ended:
                raise SessionError("helper session ended before START")
            final_admission()

    def admit_start_write(self) -> bool:
        """Admit further START bytes, or defer to pending final observation.

        Established failures remain immediately publishable. Otherwise final
        protocol accounting precedes any generic session-ended START failure.
        """
        with self._changed:
            error = self.publish_failure()
            if error is not None:
                raise error
            if self._control_observation_pending:
                return False
            if self._terminal is not None or self._control_ended:
                raise SessionError("helper session ended during START")
            return True
