# SPDX-License-Identifier: Apache-2.0
"""Immutable observations/outcomes and narrow physical effect interfaces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol


class Phase(Enum):
    CREATED = 'created'
    STARTING = 'starting'
    ACTIVE = 'active'
    RETIRING = 'retiring'
    CLOSED = 'closed'


class Stage(Enum):
    AUTHORIZED = 'authorized'
    QUEUED = 'queued'
    RUNNING = 'running'
    ADOPTED = 'adopted'
    CLEANING = 'cleaning'
    SETTLED = 'settled'


class Owner(Enum):
    PRODUCER = 'producer'
    SUPERVISOR = 'supervisor'
    NONE = 'none'


class Kind(Enum):
    START = 'START'
    STOP = 'STOP'
    EOF = 'EOF'
    INVALID = 'INVALID'
    READY = 'READY'
    EXIT = 'EXIT'
    RETRYABLE = 'RETRYABLE'
    FAILURE = 'FAILURE'
    TIMEOUT = 'TIMEOUT'
    SIGNAL = 'SIGNAL'


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    details: tuple[Diagnostic, ...] = ()

    def render(self) -> str:
        """Compatibility conversion; nested data remains canonical elsewhere."""
        return '; '.join((self.message, *(detail.render() for detail in self.details)))


class EffectFailure(Exception):
    """A physical seam returns a structured failure, not canonical notes."""

    def __init__(self, diagnostic: Diagnostic, *, retryable: bool = False) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic
        self.retryable = retryable


def diagnostic_from(exception: BaseException) -> Diagnostic:
    """Snapshot physical/legacy failure data; notes never drive decisions."""
    notes = tuple(
        Diagnostic('exception-note', note) for note in getattr(exception, '__notes__', ())
    )
    if isinstance(exception, EffectFailure):
        diagnostic = exception.diagnostic
        return replace(diagnostic, details=(*diagnostic.details, *notes)) if notes else diagnostic
    nested = (
        tuple(diagnostic_from(child) for child in exception.exceptions)
        if isinstance(exception, BaseExceptionGroup)
        else ()
    )
    return Diagnostic(type(exception).__name__, str(exception), (*nested, *notes))


@dataclass(frozen=True)
class Outcome:
    reason: str | None = None
    primary: Diagnostic | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class Fact:
    kind: Kind
    generation: int = 0
    failure: Diagnostic | None = None
    returncode: int = 0


@dataclass(frozen=True)
class ControlBatch:
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class Bulk:
    generation: int
    payload: str
    stream: str = 'stdout'


@dataclass(frozen=True)
class Frame:
    kind: str
    generation: int
    encoded: bytes


class Handle(Protocol):
    """Cleanup has a final response; a failure may leave the handle live."""

    @property
    def live(self) -> bool: ...

    async def dispose(self) -> None: ...


@dataclass
class ResourceOwner:
    """Pre-registered shared cell: one cleanup owner, no return/adopt gap.

    Producer mutation is confined to binding its physical result. Only the
    supervisor transfers the owner. Every cleanup path consults this same cell.
    """

    changed: Callable[[], None]
    owner: Owner = Owner.PRODUCER
    handle: Handle | None = None

    def bind(self, handle: Handle) -> None:
        assert self.handle is None
        self.handle = handle
        self.changed()

    def adopt(self) -> None:
        assert self.owner == Owner.PRODUCER and self.handle is not None
        self.owner = Owner.SUPERVISOR
        self.changed()


class Acquisition(Protocol):
    """begin() has entered the effect; produce() owns even a late result."""

    async def produce(self, scope: ResourceOwner) -> None: ...

    async def before_transfer(self) -> None: ...

    def transfer(self, publish: Callable[[], None]) -> None:
        """Non-suspending physical handoff seam, including interruption tests."""
        ...


class Effects(Protocol):
    async def before_receive(self) -> None: ...

    async def before_entry(self, generation: int) -> None: ...

    def begin(self, generation: int, scope: ResourceOwner) -> Acquisition: ...


class Sink(Protocol):
    async def write(self, data: bytes) -> int: ...


@dataclass(frozen=True)
class Finished:
    failure: Diagnostic | None = None
    retryable: bool = False
    cleanup: Diagnostic | None = None
    interrupted: bool = False


@dataclass(frozen=True)
class AttemptView:
    generation: int
    stage: Stage
    owner: Owner
    has_resource: bool
    live: bool
    producer_settled: bool


@dataclass(frozen=True)
class Snapshot:
    phase: Phase
    generation: int
    outcome: Outcome
    candidate: bool
    retry: bool
    barrier: str | None
    attempts: tuple[AttemptView, ...]
    protocol: tuple[Frame, ...]
    terminal_committed: bool
