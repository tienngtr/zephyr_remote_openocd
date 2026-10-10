# SPDX-License-Identifier: Apache-2.0
"""Small decision representation sketch, not a process/asyncio implementation.

Inputs describe committed observations and final physical responses. The
runtime must still supply safe custody, output admission and producer finality.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True)
class Diagnostic:
    source: str
    message: str
    details: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class ChildResult:
    returncode: int


@dataclass(frozen=True)
class Outcome:
    cause: str
    primary: Diagnostic | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    child: ChildResult | None = None

    def fail(self, diagnostic: Diagnostic) -> Outcome:
        if self.primary is None:
            return replace(self, primary=diagnostic)
        return replace(self, diagnostics=(*self.diagnostics, diagnostic))


@dataclass(frozen=True)
class Authorized:
    generation: int
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Producing:
    generation: int
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Owned:
    generation: int
    argv: tuple[str, ...]
    handle: str
    custodian: Literal['producer', 'helper'] = 'helper'


@dataclass(frozen=True)
class Settled:
    generation: int
    argv: tuple[str, ...]
    residual: str | None = None


type Attempt = Authorized | Producing | Owned | Settled


@dataclass(frozen=True)
class Created:
    argv: tuple[str, ...]
    required: frozenset[str]
    policy: Literal['live', 'exit'] = 'live'


@dataclass(frozen=True)
class Starting:
    attempt: Attempt
    required: frozenset[str]
    evidence: frozenset[str] = frozenset()
    provisional: Diagnostic | None = None
    policy: Literal['live', 'exit'] = 'live'


@dataclass(frozen=True)
class Active:
    child: Owned


@dataclass(frozen=True)
class Terminating:
    attempt: Attempt | None
    outcome: Outcome


@dataclass(frozen=True)
class Closed:
    outcome: Outcome
    residuals: tuple[str, ...] = ()


type State = Created | Starting | Active | Terminating | Closed


@dataclass(frozen=True)
class Observation:
    kind: str
    generation: int = 0
    marker: str = ''
    handle: str | None = None
    diagnostic: Diagnostic | None = None
    child: ChildResult | None = None


@dataclass(frozen=True)
class AttemptDiagnostic:
    generation: int
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Command:
    kind: str
    generation: int


@dataclass(frozen=True)
class SessionEnded:
    outcome: Outcome


type Output = AttemptDiagnostic | Command | SessionEnded


@dataclass(frozen=True)
class Decision:
    state: State
    outputs: tuple[Output, ...] = ()


def attempt_of(state: State) -> Attempt | None:
    if isinstance(state, (Starting, Terminating)):
        return state.attempt
    if isinstance(state, Active):
        return state.child
    return None


def decide(state: State, fact: Observation) -> Decision:
    attempt = attempt_of(state)
    if fact.generation and (attempt is None or fact.generation != attempt.generation):
        return Decision(state)
    if isinstance(state, Closed):
        # Local diagnostics can grow after a committed terminal snapshot;
        # there is no second SessionEnded output.
        if fact.diagnostic is not None:
            return Decision(replace(state, outcome=state.outcome.fail(fact.diagnostic)))
        return Decision(state)
    if fact.kind in ('controller-ended', 'signal', 'fatal', 'timeout'):
        outcome = state.outcome if isinstance(state, Terminating) else Outcome(fact.kind)
        if fact.diagnostic is not None:
            outcome = outcome.fail(fact.diagnostic)
        if isinstance(attempt, Authorized):
            attempt = Settled(attempt.generation, attempt.argv)
        commands: tuple[Output, ...] = (
            (Command('cleanup', attempt.generation),)
            if isinstance(attempt, Owned) and not isinstance(state, Terminating)
            else ()
        )
        return Decision(Terminating(attempt, outcome), commands)
    if isinstance(state, Created):
        if fact.kind == 'start':
            return Decision(
                Starting(Authorized(1, state.argv), state.required, policy=state.policy)
            )
        return Decision(state)
    if isinstance(state, Starting):
        if fact.kind == 'dispatch' and isinstance(attempt, Authorized):
            # Combined local admission/entry is a physical obligation, not
            # implemented by this pure function returning two output values.
            return Decision(
                replace(state, attempt=Producing(attempt.generation, attempt.argv)),
                (
                    AttemptDiagnostic(attempt.generation, attempt.argv),
                    Command('spawn', attempt.generation),
                ),
            )
        if fact.kind == 'producer-final' and isinstance(attempt, Producing):
            if fact.handle is not None:
                return Decision(
                    replace(state, attempt=Owned(attempt.generation, attempt.argv, fact.handle))
                )
            assert fact.diagnostic is not None
            return Decision(
                replace(
                    state,
                    attempt=Settled(attempt.generation, attempt.argv),
                    provisional=fact.diagnostic,
                )
            )
        if fact.kind == 'retryable' and isinstance(attempt, Owned):
            return Decision(
                replace(state, provisional=fact.diagnostic, evidence=frozenset()),
                (Command('cleanup', attempt.generation),),
            )
        if fact.kind == 'cleanup-final' and isinstance(attempt, Owned):
            assert state.provisional is not None
            if fact.diagnostic is not None:
                return Decision(
                    Terminating(
                        Settled(attempt.generation, attempt.argv, attempt.handle),
                        Outcome('failure', state.provisional).fail(fact.diagnostic),
                    )
                )
            return Decision(replace(state, attempt=Settled(attempt.generation, attempt.argv)))
        if fact.kind == 'retry' and isinstance(attempt, Settled) and state.provisional is not None:
            if attempt.generation >= 2:
                return Decision(Terminating(attempt, Outcome('failure', state.provisional)))
            return Decision(
                Starting(
                    Authorized(attempt.generation + 1, attempt.argv),
                    state.required,
                    policy=state.policy,
                )
            )
        if fact.kind == 'marker' and state.provisional is None:
            return Decision(replace(state, evidence=state.evidence | {fact.marker}))
        if (
            fact.kind == 'ready-admitted'
            and state.policy == 'live'
            and isinstance(attempt, Owned)
            and state.provisional is None
            and state.required <= state.evidence
        ):
            return Decision(Active(attempt))
    if isinstance(state, Terminating):
        if fact.kind == 'producer-final' and isinstance(attempt, Producing):
            if fact.handle is not None:
                return Decision(
                    replace(
                        state,
                        attempt=Owned(attempt.generation, attempt.argv, fact.handle, 'producer'),
                    ),
                    (Command('cleanup', attempt.generation),),
                )
            outcome = state.outcome.fail(fact.diagnostic) if fact.diagnostic else state.outcome
            return Decision(Terminating(Settled(attempt.generation, attempt.argv), outcome))
        if fact.kind == 'cleanup-final' and isinstance(attempt, Owned):
            outcome = state.outcome.fail(fact.diagnostic) if fact.diagnostic else state.outcome
            return Decision(
                Terminating(
                    Settled(
                        attempt.generation,
                        attempt.argv,
                        attempt.handle if fact.diagnostic else None,
                    ),
                    outcome,
                )
            )
        if fact.kind == 'finalize' and (attempt is None or isinstance(attempt, Settled)):
            residuals = (
                (attempt.residual,) if isinstance(attempt, Settled) and attempt.residual else ()
            )
            return Decision(Closed(state.outcome, residuals), (SessionEnded(state.outcome),))
    if fact.kind == 'child-exit' and isinstance(attempt, Owned):
        assert fact.child is not None
        outcome = state.outcome if isinstance(state, Terminating) else Outcome('process-exit')
        outcome = replace(outcome, child=fact.child)
        if isinstance(state, Starting) and state.policy == 'live':
            outcome = outcome.fail(Diagnostic('child', 'exit before readiness'))
        elif fact.child.returncode != 0 and not isinstance(state, Terminating):
            outcome = outcome.fail(Diagnostic('openocd', f'process exited {fact.child.returncode}'))
        # A leader exit result does not prove descendants/relay ownership settled.
        commands = (
            () if isinstance(state, Terminating) else (Command('cleanup', attempt.generation),)
        )
        return Decision(Terminating(attempt, outcome), commands)
    return Decision(state)


@dataclass(frozen=True)
class Opening:
    ready: bool = False
    forwarded: bool = False


@dataclass(frozen=True)
class LocalActive:
    pass


@dataclass(frozen=True)
class Cancelling:
    outcome: Outcome


type Local = Opening | LocalActive | Cancelling


def local_failure(state: Local, diagnostic: Diagnostic) -> Cancelling:
    outcome = state.outcome if isinstance(state, Cancelling) else Outcome('failure')
    return Cancelling(outcome.fail(diagnostic))


def merge_terminal(state: Local, remote: Outcome) -> Outcome:
    outcome = state.outcome if isinstance(state, Cancelling) else Outcome(remote.cause)
    if remote.primary is not None:
        outcome = outcome.fail(remote.primary)
    return replace(
        outcome, diagnostics=(*outcome.diagnostics, *remote.diagnostics), child=remote.child
    )


def local_decide(state: Local, event: str) -> tuple[Local, bool]:
    if event == 'cancel':
        return (state if isinstance(state, Cancelling) else Cancelling(Outcome('requested'))), False
    if isinstance(state, Opening):
        if event == 'ready':
            return replace(state, ready=True), False
        if event == 'forwarded':
            return replace(state, forwarded=True), False
        if event == 'launch' and state.ready and state.forwarded:
            return LocalActive(), True
    return state, False
