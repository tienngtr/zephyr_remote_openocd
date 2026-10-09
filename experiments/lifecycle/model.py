# SPDX-License-Identifier: Apache-2.0
"""Pure lifecycle decisions; tokens stand for effects, never real resources."""

from dataclasses import dataclass, replace
from enum import Enum, auto


class Phase(Enum):
    CREATED = auto()
    STARTING = auto()
    ACTIVE = auto()
    RETIRING = auto()
    CLOSED = auto()


class Command(Enum):
    START = auto()
    STOP = auto()
    CONTROL_EOF = auto()
    TERMINATION_SIGNAL = auto()
    CLOSE = auto()


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    details: tuple['Failure', ...] = ()


@dataclass(frozen=True)
class ControlFailed:
    failure: Failure


@dataclass(frozen=True)
class ControlBatch:
    """Consumed control facts admitted without intermediate effect dispatch."""

    commands: tuple[Command | ControlFailed, ...]


@dataclass(frozen=True)
class Outcome:
    reason: str
    returncode: int | None = None
    primary: Failure | None = None
    diagnostics: tuple[Failure, ...] = ()


@dataclass(frozen=True)
class ResourceKey:
    generation: int
    name: str


class Stage(Enum):
    PENDING = auto()
    OFFERED = auto()
    HELD = auto()
    CLEANING = auto()
    DONE = auto()


class Owner(Enum):
    EFFECT = auto()
    SESSION = auto()


@dataclass(frozen=True)
class Resource:
    key: ResourceKey
    stage: Stage = Stage.PENDING
    owner: Owner = Owner.EFFECT


@dataclass(frozen=True)
class Acquire:
    key: ResourceKey


@dataclass(frozen=True)
class Acquired:
    """ChildSpawned is Acquired(ResourceKey(generation, 'child'))."""

    key: ResourceKey


@dataclass(frozen=True)
class Adopt:
    key: ResourceKey


@dataclass(frozen=True)
class StartupFailed:
    generation: int
    failure: Failure
    retryable: bool = False
    failed_acquisition: ResourceKey | None = None


@dataclass(frozen=True)
class ReadinessCandidate:
    generation: int


@dataclass(frozen=True)
class CommitReady:
    generation: int


@dataclass(frozen=True)
class ChildExited:
    generation: int
    returncode: int
    bind_collision: bool = False


@dataclass(frozen=True)
class StartupTimeout:
    generation: int


@dataclass(frozen=True)
class CommitRetry:
    generation: int


@dataclass(frozen=True)
class CleanupFinished:
    key: ResourceKey
    failure: Failure | None = None


type Event = (
    Command
    | ControlBatch
    | ControlFailed
    | Acquire
    | Acquired
    | Adopt
    | StartupFailed
    | ReadinessCandidate
    | CommitReady
    | ChildExited
    | StartupTimeout
    | CommitRetry
    | CleanupFinished
)


@dataclass(frozen=True)
class Effect:
    kind: str
    key: ResourceKey
    owner: Owner = Owner.EFFECT


@dataclass(frozen=True)
class Output:
    """Semantic protocol output, not a complete Protocol v1 wire frame."""

    kind: str
    generation: int = 0
    outcome: Outcome | None = None


@dataclass(frozen=True)
class State:
    phase: Phase = Phase.CREATED
    generation: int = 0
    attempt_limit: int = 2
    ready_candidate: bool = False
    attempt_failure: Failure | None = None
    retryable: bool = False
    outcome: Outcome | None = None
    cleanup_started: bool = False
    resources: tuple[Resource, ...] = ()
    outbox: tuple[Effect | Output, ...] = (Output('SESSION_CREATED'),)


def _resource(state: State, key: ResourceKey) -> Resource | None:
    return next((resource for resource in state.resources if resource.key == key), None)


def _update_resource(state: State, resource: Resource) -> State:
    return replace(
        state,
        resources=tuple(resource if item.key == resource.key else item for item in state.resources),
    )


def _start(state: State) -> State:
    generation = state.generation + 1
    child = Resource(ResourceKey(generation, 'child'))
    return replace(
        state,
        phase=Phase.STARTING,
        generation=generation,
        ready_candidate=False,
        attempt_failure=None,
        retryable=False,
        cleanup_started=False,
        resources=(*state.resources, child),
        outbox=(*state.outbox, Output('PROCESS_STARTING', generation), Effect('SPAWN', child.key)),
    )


def _terminate(state: State, outcome: Outcome) -> State:
    if state.outcome is not None:
        return state
    return replace(state, phase=Phase.RETIRING, ready_candidate=False, outcome=outcome)


def _fail_attempt(state: State, failure: Failure, retryable: bool) -> State:
    retryable = retryable and state.generation < state.attempt_limit
    state = replace(
        state,
        phase=Phase.RETIRING,
        ready_candidate=False,
        attempt_failure=failure,
        retryable=retryable,
    )
    if retryable:
        return state
    return _terminate(state, Outcome('failure', primary=failure))


def _clean(state: State, resource: Resource) -> State:
    state = _update_resource(state, replace(resource, stage=Stage.CLEANING))
    return replace(state, outbox=(*state.outbox, Effect('CLEAN', resource.key, resource.owner)))


def _settle(state: State) -> State:
    """Only settled effect tickets permit terminal publication or retry."""
    if (
        state.phase == Phase.RETIRING
        and state.cleanup_started
        and state.outcome is not None
        and all(resource.stage == Stage.DONE for resource in state.resources)
    ):
        # main has no orderly wire event for EOF/signal-only termination.
        if state.outcome.primary is None and state.outcome.reason in ('eof', 'signal'):
            return replace(state, phase=Phase.CLOSED)
        kind = 'ERROR' if state.outcome.primary is not None else 'SESSION_CLOSED'
        return replace(
            state,
            phase=Phase.CLOSED,
            outbox=(*state.outbox, Output(kind, outcome=state.outcome)),
        )
    return state


def transition(state: State, event: Event) -> State:
    """Prepare one immutable state/outbox; no externally visible partial changes."""
    if isinstance(event, ControlBatch):
        for command in event.commands:
            state = transition(state, command)
        return state
    if state.phase == Phase.CLOSED:
        return state
    if isinstance(event, ControlFailed):
        return _terminate(state, Outcome('failure', primary=event.failure))
    if isinstance(event, Command):
        if event == Command.START and state.outcome is None:
            if state.phase == Phase.CREATED:
                return _start(state)
            return _terminate(
                state, Outcome('failure', primary=Failure('PROTOCOL', 'duplicate START'))
            )
        if event in (Command.STOP, Command.CONTROL_EOF, Command.TERMINATION_SIGNAL):
            reason = {
                Command.STOP: 'requested',
                Command.CONTROL_EOF: 'eof',
                Command.TERMINATION_SIGNAL: 'signal',
            }[event]
            return _terminate(state, Outcome(reason))
        if event == Command.CLOSE and state.phase == Phase.RETIRING:
            state = replace(state, cleanup_started=True)
            for owned in state.resources:
                if owned.stage in (Stage.OFFERED, Stage.HELD):
                    state = _clean(state, owned)
            return _settle(state)
        return state
    if isinstance(event, Acquire):
        if (
            state.phase == Phase.STARTING
            and event.key.generation == state.generation
            and _resource(state, event.key) is None
        ):
            return replace(
                state,
                resources=(*state.resources, Resource(event.key)),
                outbox=(*state.outbox, Effect('ACQUIRE', event.key)),
            )
        return state
    if isinstance(event, Acquired):
        resource = _resource(state, event.key)
        if resource is None or resource.stage != Stage.PENDING:
            return state
        resource = replace(resource, stage=Stage.OFFERED)
        state = _update_resource(state, resource)
        if state.cleanup_started:
            state = _clean(state, resource)
        return state
    if isinstance(event, Adopt):
        resource = _resource(state, event.key)
        if (
            resource is not None
            and resource.stage == Stage.OFFERED
            and event.key.generation == state.generation
            and state.phase == Phase.STARTING
        ):
            return _update_resource(state, replace(resource, stage=Stage.HELD, owner=Owner.SESSION))
        return state
    if isinstance(event, CleanupFinished):
        resource = _resource(state, event.key)
        if resource is None or resource.stage != Stage.CLEANING:
            return state
        state = _update_resource(state, replace(resource, stage=Stage.DONE))
        if event.failure is not None:
            # A retryable attempt failure is provisional, not an established
            # session primary. main selects the cleanup failure in that case.
            outcome = state.outcome or Outcome('failure')
            outcome = replace(
                outcome,
                primary=outcome.primary or event.failure,
                diagnostics=(*outcome.diagnostics, event.failure),
            )
            state = replace(state, outcome=outcome)
        return _settle(state)
    if isinstance(event, StartupFailed) and event.failed_acquisition is not None:
        resource = _resource(state, event.failed_acquisition)
        if (
            resource is None
            or resource.stage != Stage.PENDING
            or event.failed_acquisition.generation != event.generation
        ):
            return state
        state = _update_resource(state, replace(resource, stage=Stage.DONE))
        if event.generation != state.generation or state.outcome is not None:
            return _settle(state)
    # Attempt facts/commit requests are fenced before touching lifecycle state.
    if event.generation != state.generation or state.outcome is not None:
        return state
    if isinstance(event, CommitRetry):
        if (
            state.phase == Phase.RETIRING
            and state.retryable
            and state.cleanup_started
            and all(resource.stage == Stage.DONE for resource in state.resources)
        ):
            return _start(state)
        return state
    if state.phase not in (Phase.STARTING, Phase.ACTIVE):
        return state
    if isinstance(event, StartupFailed):
        if state.phase != Phase.STARTING:
            return state
        return _fail_attempt(state, event.failure, event.retryable)
    if isinstance(event, ChildExited):
        if state.phase == Phase.ACTIVE:
            return _terminate(state, Outcome('process_exit', event.returncode))
        return _fail_attempt(
            state,
            Failure('STARTUP_EXIT', f'child exited with {event.returncode}'),
            event.bind_collision,
        )
    if isinstance(event, StartupTimeout) and state.phase == Phase.STARTING:
        return _fail_attempt(state, Failure('TIMEOUT', 'startup timed out'), False)
    if isinstance(event, ReadinessCandidate) and state.phase == Phase.STARTING:
        return replace(state, ready_candidate=True)
    if isinstance(event, CommitReady) and state.phase == Phase.STARTING and state.ready_candidate:
        child = _resource(state, ResourceKey(state.generation, 'child'))
        if child is not None and child.stage == Stage.HELD:
            return replace(
                state,
                phase=Phase.ACTIVE,
                ready_candidate=False,
                outbox=(*state.outbox, Output('PROCESS_READY', state.generation)),
            )
    return state


class Supervisor:
    """One authority publishes a state and its outbox with one reference store."""

    def __init__(self, attempt_limit: int = 2) -> None:
        self._state = State(attempt_limit=attempt_limit)

    @property
    def state(self) -> State:
        """Readers see only immutable committed snapshots."""
        return self._state

    def accept(self, event: Event) -> State:
        """STOP, READY, adoption, and retry linearize at this publication."""
        prepared = transition(self.state, event)
        self._state = prepared
        return prepared
