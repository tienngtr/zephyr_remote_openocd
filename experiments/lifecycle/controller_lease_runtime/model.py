# SPDX-License-Identifier: Apache-2.0
"""Tagged session decisions and structured results, independent of Unix I/O."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .unix import Ticket


def weakened(rule: str) -> bool:
    """Deliberate experimental mutations, never a product configuration option."""
    return os.environ.get('ZRO_LEASE_MUTATION') == rule


@dataclass(frozen=True)
class Diagnostic:
    source: str
    message: str
    details: tuple[Diagnostic, ...] = ()
    child: ChildResult | None = None


def failure_record(source: str, error: BaseException) -> Diagnostic:
    """Convert physical/boundary exceptions; never select lifecycle precedence."""
    details = tuple(Diagnostic('detail', note) for note in getattr(error, '__notes__', ()))
    if isinstance(error, BaseExceptionGroup):
        details = (*tuple(failure_record(source, child) for child in error.exceptions), *details)
    return Diagnostic(source, str(error) or type(error).__name__, details)


@dataclass(frozen=True)
class ChildResult:
    generation: int
    returncode: int


@dataclass(frozen=True)
class Outcome:
    trigger: str
    primary: Diagnostic | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    child: ChildResult | None = None

    def fail(self, failure: Diagnostic) -> Outcome:
        if self.primary is None or (failure.source == 'cleanup' and weakened('replace-primary')):
            return replace(self, primary=failure)
        return replace(self, diagnostics=(*self.diagnostics, failure))


@dataclass(frozen=True)
class Request:
    argv: tuple[str, ...]
    required: frozenset[str]
    policy: Literal['live', 'exit'] = 'live'
    max_attempts: int = 2
    pass_fds: tuple[int, ...] = ()


@dataclass(frozen=True)
class Authorized:
    generation: int


@dataclass(frozen=True)
class Producing:
    ticket: Ticket

    @property
    def generation(self) -> int:
        return self.ticket.generation


@dataclass(frozen=True)
class Owned:
    ticket: Ticket

    @property
    def generation(self) -> int:
        return self.ticket.generation


@dataclass(frozen=True)
class Settled:
    generation: int


type Attempt = Authorized | Producing | Owned | Settled


@dataclass(frozen=True)
class Created:
    pass


@dataclass(frozen=True)
class Starting:
    request: Request
    attempt: Attempt
    evidence: frozenset[str] = frozenset()
    provisional: Diagnostic | None = None
    history: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class Active:
    request: Request
    child: Owned
    history: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class Terminating:
    attempt: Attempt | None
    outcome: Outcome


@dataclass(frozen=True)
class Closed:
    outcome: Outcome
    residuals: tuple[str, ...]
    snapshot: Outcome


@dataclass(frozen=True)
class TerminalResult:
    outcome: Outcome
    disposal_confirmed: bool


type State = Created | Starting | Active | Terminating | Closed


def attempt_of(state: State) -> Attempt | None:
    if isinstance(state, (Starting, Terminating)):
        return state.attempt
    if isinstance(state, Active):
        return state.child
    return None


@dataclass(frozen=True)
class Start:
    request: Request


@dataclass(frozen=True)
class ControllerEnded:
    pass


@dataclass(frozen=True)
class Marker:
    generation: int
    name: str


@dataclass(frozen=True)
class Exited:
    generation: int
    returncode: int


@dataclass(frozen=True)
class Offered:
    ticket: Ticket


@dataclass(frozen=True)
class Cleaned:
    ticket: Ticket
    failures: tuple[Diagnostic, ...]
    disposed: bool


@dataclass(frozen=True)
class Failure:
    diagnostic: Diagnostic
    generation: int | None = None


@dataclass(frozen=True)
class Signal:
    signum: int


@dataclass(frozen=True)
class Deadline:
    generation: int


type Fact = (
    Start | ControllerEnded | Marker | Exited | Offered | Cleaned | Failure | Signal | Deadline
)
