# SPDX-License-Identifier: Apache-2.0

"""Pure local launch foundation for the later client/session adaptation.

The coordinator must account for recorded fatal observations at execution
entry through the same authority boundary. This gate has no synchronization
with the Protocol v1 reader and is not integrated into the current session.
"""

from collections.abc import Iterable
from enum import Enum, auto

from .model import Service
from .outcome import Diagnostic


class LocalPhase(Enum):
    OPENING = auto()
    ACTIVE = auto()
    CANCELLING = auto()
    ENDED = auto()


class LaunchDenied(RuntimeError):
    """A cancelled or ended operation cannot prepare another launch."""


class LaunchGate:
    """The local coordinator alone prepares, enters, cancels, and ends launches.

    READY and forwarding are observations, not transitions. A generation scopes
    each boundary, including GDB setup followed by an RTT client. Scheduling
    authorization is not entry: the token must be checked at actual execution.
    This object owns no subprocess, transport, socket, or cleanup obligation.
    """

    def __init__(self) -> None:
        self._phase = LocalPhase.OPENING
        self._generation: int | None = None
        self._required: frozenset[Service] = frozenset()
        self._forwarded: frozenset[Service] = frozenset()
        self._remote_ready = False
        self._failure: Diagnostic | None = None

    @property
    def phase(self) -> LocalPhase:
        return self._phase

    @property
    def failure(self) -> Diagnostic | None:
        return self._failure

    def prepare(self, required_services: Iterable[Service]) -> int:
        if self._phase in (LocalPhase.CANCELLING, LocalPhase.ENDED):
            raise LaunchDenied("operation no longer permits dependent launch")
        self._generation = (self._generation or 0) + 1
        self._required = frozenset(required_services)
        self._phase = LocalPhase.OPENING
        return self._generation

    def observe_remote_ready(self) -> None:
        self._remote_ready = True

    def observe_forwarded(self, services: Iterable[Service]) -> None:
        self._forwarded = frozenset(services)

    def enter(self, generation: int) -> bool:
        if (
            self._phase != LocalPhase.OPENING
            or self._generation != generation
            or not self._remote_ready
            or not self._required.issubset(self._forwarded)
        ):
            return False
        self._phase = LocalPhase.ACTIVE
        return True

    def cancel(self) -> None:
        if self._phase != LocalPhase.ENDED:
            self._phase = LocalPhase.CANCELLING

    def fail(self, failure: Diagnostic) -> None:
        if self._failure is None:
            self._failure = failure
        self.cancel()

    def end(self) -> None:
        self._phase = LocalPhase.ENDED
