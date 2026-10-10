# SPDX-License-Identifier: Apache-2.0

"""Pure remote lifecycle authority for the controller-lease implementation.

The coordinator alone calls transitions. Physical adapters retain their own
resources and report facts: these records neither acquire nor dispose resources.
Admission, live-child checks, and settlement arguments attest completed adapter
work, not requests to perform it. The helper coordinator attests these boundaries
after its physical adapters complete the corresponding work.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from .outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    Outcome,
    TerminalSnapshot,
    Trigger,
)

MAX_ATTEMPT_GENERATIONS = 32


class StartupRequest(Protocol):
    """Read-only startup policy on an immutable validated request."""

    @property
    def completion_policy(self) -> CompletionPolicy: ...

    @property
    def required_output_sentinels(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class AuthorizedAttempt:
    generation: int


@dataclass(frozen=True, slots=True)
class ProducingAttempt:
    generation: int
    termination_requested: bool = False
    child_result: ChildResult | None = None


@dataclass(frozen=True, slots=True)
class OwnedAttempt:
    generation: int
    termination_requested: bool = False
    child_result: ChildResult | None = None


@dataclass(frozen=True, slots=True)
class SettledAttempt:
    generation: int
    child_result: ChildResult | None


Attempt = AuthorizedAttempt | ProducingAttempt | OwnedAttempt | SettledAttempt


@dataclass(frozen=True, slots=True)
class Created:
    pass


@dataclass(frozen=True, slots=True)
class Starting[RequestT: StartupRequest]:
    request: RequestT
    attempt: Attempt
    evidence: frozenset[str] = frozenset()
    # Only a failure classified safely repeatable may remain provisional.
    provisional_failure: Diagnostic | None = None


@dataclass(frozen=True, slots=True)
class Active[RequestT: StartupRequest]:
    request: RequestT
    attempt: OwnedAttempt


@dataclass(frozen=True, slots=True)
class Terminating:
    attempt: Attempt | None
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class Closed:
    attempt: Attempt | None
    snapshot: TerminalSnapshot
    local_diagnostics: tuple[Diagnostic, ...] = ()


type RemotePhase[RequestT: StartupRequest] = (
    Created | Starting[RequestT] | Active[RequestT] | Terminating | Closed
)


class LifecycleError(RuntimeError):
    """A coordinator requested an invalid lifecycle operation."""


class RemoteLifecycle[RequestT: StartupRequest]:
    """One transition authority; immutable phase data prevents shadow state."""

    def __init__(self) -> None:
        self._state: RemotePhase[RequestT] = Created()

    @property
    def state(self) -> RemotePhase[RequestT]:
        return self._state

    def start(self, request: RequestT) -> int:
        if not isinstance(self._state, Created):
            raise LifecycleError("START is valid only in Created")
        self._state = Starting(request, AuthorizedAttempt(1))
        return 1

    def _current_attempt(self, generation: int) -> Attempt | None:
        state = self._state
        attempt = None if isinstance(state, Created) else state.attempt
        return attempt if attempt is not None and attempt.generation == generation else None

    def _replace_attempt(self, attempt: Attempt) -> None:
        state = self._state
        if isinstance(state, (Starting, Terminating)):
            self._state = replace(state, attempt=attempt)
        elif isinstance(state, Active) and isinstance(attempt, OwnedAttempt):
            self._state = Active(state.request, attempt)
        else:
            raise LifecycleError("attempt update is incompatible with the phase")

    def enter_attempt(self, generation: int, *, admitted: bool) -> bool:
        """Validate physical entry after exact argv admission and before spawn."""
        if not isinstance(self._state, Starting) or not isinstance(
            self._current_attempt(generation), AuthorizedAttempt
        ):
            return False
        if not admitted:
            self.terminate(
                Trigger.OUTPUT_FAILURE, Diagnostic("ATTEMPT_ADMISSION", "admission failed")
            )
            return False
        self._replace_attempt(ProducingAttempt(generation))
        return True

    def adopt_attempt(self, generation: int) -> bool:
        """Late production remains accounted for during termination, never active."""
        attempt = self._current_attempt(generation)
        if not isinstance(attempt, ProducingAttempt) or not isinstance(
            self._state, (Starting, Terminating)
        ):
            return False
        owned = OwnedAttempt(generation, attempt.termination_requested, attempt.child_result)
        self._replace_attempt(owned)
        state = self._state
        if (
            isinstance(state, Starting)
            and state.request.completion_policy == CompletionPolicy.PROCESS_EXIT
        ):
            if owned.child_result is None:
                self._state = Active(state.request, owned)
            else:
                self.terminate(Trigger.CHILD_EXIT)
        return True

    def observe_marker(self, generation: int, marker: str) -> bool:
        state = self._state
        if (
            not isinstance(state, Starting)
            or state.request.completion_policy != CompletionPolicy.LIVE_SERVER
        ):
            return False
        attempt = self._current_attempt(generation)
        if not isinstance(attempt, OwnedAttempt) or attempt.child_result is not None:
            return False
        if marker not in state.request.required_output_sentinels:
            return False
        self._state = replace(state, evidence=state.evidence | {marker})
        return True

    def ready(self, generation: int, *, child_live: bool, admitted: bool) -> bool:
        state = self._state
        attempt = self._current_attempt(generation)
        if (
            not isinstance(state, Starting)
            or state.request.completion_policy != CompletionPolicy.LIVE_SERVER
            or not isinstance(attempt, OwnedAttempt)
            or attempt.child_result is not None
            or state.provisional_failure is not None
            or not set(state.request.required_output_sentinels).issubset(state.evidence)
            or not child_live
        ):
            return False
        if not admitted:
            self.terminate(
                Trigger.OUTPUT_FAILURE, Diagnostic("READY_ADMISSION", "admission failed")
            )
            return False
        self._state = Active(state.request, attempt)
        return True

    def observe_child_exit(self, generation: int, returncode: int) -> bool:
        attempt = self._current_attempt(generation)
        if not isinstance(attempt, (ProducingAttempt, OwnedAttempt)) or isinstance(
            self._state, Closed
        ):
            return False
        if attempt.child_result is not None:
            return False
        result = ChildResult(generation, returncode, attempt.termination_requested)
        self._replace_attempt(replace(attempt, child_result=result))
        state = self._state
        if isinstance(state, Terminating):
            self._state = replace(state, outcome=state.outcome.with_child_result(result))
        elif isinstance(state, Active):
            self.terminate(Trigger.CHILD_EXIT)
        return True

    def record_child_termination(self, generation: int) -> bool:
        """Record actual cleanup-signal delivery, after checking observable exit."""
        attempt = self._current_attempt(generation)
        if not isinstance(self._state, Terminating) or not isinstance(
            attempt, (ProducingAttempt, OwnedAttempt)
        ):
            return False
        if attempt.child_result is not None:
            return False
        self._replace_attempt(replace(attempt, termination_requested=True))
        return True

    def classify_startup_failure(
        self, generation: int, failure: Diagnostic, *, safely_repeatable: bool
    ) -> bool:
        state = self._state
        attempt = self._current_attempt(generation)
        if (
            not isinstance(state, Starting)
            or state.request.completion_policy != CompletionPolicy.LIVE_SERVER
            or attempt is None
            or state.provisional_failure is not None
        ):
            return False
        if not safely_repeatable:
            self.terminate(Trigger.STARTUP_FAILURE, failure)
            return True
        if (
            not isinstance(attempt, (ProducingAttempt, OwnedAttempt))
            or attempt.child_result is None
        ):
            return False
        self._state = replace(state, provisional_failure=failure)
        return True

    def settle_attempt(
        self, generation: int, *, producer_quiescent: bool, resources_disposed: bool
    ) -> bool:
        """Cancellation and wait expiry cannot supply either settlement fact."""
        attempt = self._current_attempt(generation)
        if (
            attempt is None
            or not isinstance(self._state, (Starting, Terminating))
            or not producer_quiescent
            or not resources_disposed
        ):
            return False
        result = None if isinstance(attempt, AuthorizedAttempt) else attempt.child_result
        self._replace_attempt(SettledAttempt(generation, result))
        return True

    def retry(self, generation: int) -> int | None:
        state = self._state
        if not isinstance(state, Starting) or not isinstance(
            self._current_attempt(generation), SettledAttempt
        ):
            return None
        failure = state.provisional_failure
        if failure is None:
            return None
        if generation >= MAX_ATTEMPT_GENERATIONS:
            self.terminate(Trigger.STARTUP_FAILURE, failure)
            return None
        next_generation = generation + 1
        self._state = Starting(state.request, AuthorizedAttempt(next_generation))
        return next_generation

    def terminate(self, trigger: Trigger, failure: Diagnostic | None = None) -> None:
        state = self._state
        attempt = None if isinstance(state, Created) else state.attempt
        result = (
            None
            if attempt is None or isinstance(attempt, AuthorizedAttempt)
            else attempt.child_result
        )
        # Validate the incoming cause even when another trigger is established.
        provisional = state.provisional_failure if isinstance(state, Starting) else None
        # A winning termination retains the classified attempt failure as detail;
        # retry exhaustion already promotes that same failure to primary.
        diagnostics = (provisional,) if provisional is not None and provisional != failure else ()
        outcome = Outcome(trigger, failure, diagnostics, child_result=result)
        if isinstance(state, Closed):
            if failure is not None:
                self._state = replace(state, local_diagnostics=(*state.local_diagnostics, failure))
        elif isinstance(state, Terminating):
            if failure is not None:
                self._state = replace(state, outcome=state.outcome.with_failure(failure))
        else:
            self._state = Terminating(attempt, outcome)

    def freeze(self, cleanup: CleanupReport) -> TerminalSnapshot:
        """Freeze once, independently of terminal admission or delivery."""
        state = self._state
        if isinstance(state, Closed):
            return state.snapshot
        if not isinstance(state, Terminating):
            raise LifecycleError("terminal snapshot requires termination")
        if (
            isinstance(state.attempt, (ProducingAttempt, OwnedAttempt))
            and cleanup.child_disposal != "unconfirmed"
        ):
            raise LifecycleError("unsettled attempt cannot claim confirmed disposal")
        snapshot = TerminalSnapshot(state.outcome, cleanup)
        self._state = Closed(state.attempt, snapshot)
        return snapshot
