# SPDX-License-Identifier: Apache-2.0

"""Pure local launch eligibility for the controller-lease client.

The coordinator must account for recorded fatal observations at execution
entry through the same authority boundary. The session observations serialize
reader facts and execution entry through one shared authority; this pure gate
itself owns no synchronization.
"""

from collections.abc import Iterable
from enum import Enum, auto

from zephyr_remote_openocd.controller.model import Service
from zephyr_remote_openocd.protocol.outcome import Diagnostic


class AdmissionPhase(Enum):
    OPENING = auto()
    ACTIVE = auto()
    CANCELLING = auto()
    ENDED = auto()


class LaunchDenied(RuntimeError):
    """A cancelled or ended operation cannot prepare another launch."""


class AdmissionGate:
    """The local coordinator alone prepares, enters, cancels, and ends launches.

    READY and forwarding are observations, not transitions. A launch ticket scopes
    each boundary, including GDB setup followed by an RTT client. Scheduling
    authorization is not entry: the token must be checked at actual execution.
    This object owns no subprocess, transport, socket, or cleanup obligation.
    """

    def __init__(self) -> None:
        self._phase = AdmissionPhase.OPENING
        self._ticket: int | None = None
        self._required: frozenset[Service] = frozenset()
        self._forwarded: frozenset[Service] = frozenset()
        self._remote_ready = False
        self._failure: Diagnostic | None = None

    @property
    def phase(self) -> AdmissionPhase:
        return self._phase

    @property
    def failure(self) -> Diagnostic | None:
        return self._failure

    def prepare(self, required_services: Iterable[Service]) -> int:
        if self._phase in (AdmissionPhase.CANCELLING, AdmissionPhase.ENDED):
            raise LaunchDenied("operation no longer permits dependent launch")
        self._ticket = (self._ticket or 0) + 1
        self._required = frozenset(required_services)
        self._phase = AdmissionPhase.OPENING
        return self._ticket

    def observe_remote_ready(self) -> None:
        self._remote_ready = True

    def observe_forwarded(self, services: Iterable[Service]) -> None:
        self._forwarded = frozenset(services)

    def enter(self, ticket: int) -> bool:
        if (
            self._phase != AdmissionPhase.OPENING
            or self._ticket != ticket
            or not self._remote_ready
            or not self._required.issubset(self._forwarded)
        ):
            return False
        self._phase = AdmissionPhase.ACTIVE
        return True

    def cancel(self) -> None:
        if self._phase != AdmissionPhase.ENDED:
            self._phase = AdmissionPhase.CANCELLING

    def fail(self, failure: Diagnostic) -> None:
        if self._failure is None:
            self._failure = failure
        self.cancel()

    def end(self) -> None:
        self._phase = AdmissionPhase.ENDED
