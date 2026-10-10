# SPDX-License-Identifier: Apache-2.0

"""Immutable lifecycle results, independent of exceptions and transport status.

These are internal foundations for the controller-lease cutover. Protocol v1
continues to use its existing result path until client and helper change together.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal


class CompletionPolicy(StrEnum):
    LIVE_SERVER = "live_server"
    PROCESS_EXIT = "process_exit"


class Trigger(StrEnum):
    CONTROLLER_EOF = "controller_eof"
    SIGNAL = "signal"
    CHILD_EXIT = "child_exit"
    STARTUP_FAILURE = "startup_failure"
    PROTOCOL_FAILURE = "protocol_failure"
    OUTPUT_FAILURE = "output_failure"
    HELPER_FAILURE = "helper_failure"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    @classmethod
    def from_exception(cls, code: str, error: BaseException) -> Diagnostic:
        """Capture legacy boundary detail without retaining mutable exceptions."""
        return cls(
            code,
            str(error),
            tuple(cls("EXCEPTION_NOTE", note) for note in getattr(error, "__notes__", ())),
        )


@dataclass(frozen=True, slots=True)
class ChildResult:
    """Only child observation may supply this status and its signal context."""

    generation: int
    returncode: int
    termination_requested: bool


@dataclass(frozen=True, slots=True)
class Outcome:
    trigger: Trigger
    primary_failure: Diagnostic | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    child_result: ChildResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        if (
            self.trigger not in (Trigger.CONTROLLER_EOF, Trigger.CHILD_EXIT)
            and self.primary_failure is None
        ):
            raise ValueError("failure triggers require a primary failure")
        if self.trigger == Trigger.CHILD_EXIT and self.child_result is None:
            raise ValueError("child-exit termination requires an observed child result")

    def with_failure(self, failure: Diagnostic) -> Outcome:
        """Establish the first failure; preserve later failures in arrival order."""
        if self.primary_failure is None:
            return replace(self, primary_failure=failure)
        return replace(self, diagnostics=(*self.diagnostics, failure))

    def with_child_result(self, result: ChildResult) -> Outcome:
        if self.child_result is not None and self.child_result != result:
            raise ValueError("an observed child result cannot be replaced")
        return replace(self, child_result=result)

    def operation_failed(self, policy: CompletionPolicy) -> bool:
        """Interpret child provenance separately from established failures.

        Required disposal and independent helper/SSH status must also be checked
        by the boundary. A requested server shutdown may have a nonzero child
        status; one-shot completion must be genuine and successful.
        """
        if self.primary_failure is not None:
            return True
        result = self.child_result
        if policy == CompletionPolicy.PROCESS_EXIT:
            return result is None or result.termination_requested or result.returncode != 0
        return result is not None and not result.termination_requested and result.returncode != 0


ResidualResource = Literal[
    "child_producer",
    "child_group",
    "child_relays",
    "address_lease",
    "workspace",
    "workspace_metadata",
]


@dataclass(frozen=True, slots=True)
class CleanupReport:
    child_disposal: Literal["not_acquired", "confirmed", "unconfirmed"]
    workspace_disposal: Literal["not_created", "confirmed", "unconfirmed"]
    residual_resources: tuple[ResidualResource, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "residual_resources", tuple(self.residual_resources))
        residuals = set(self.residual_resources)
        if len(residuals) != len(self.residual_resources):
            raise ValueError("residual obligations must be unique")
        child_residuals = residuals - {"workspace", "workspace_metadata"}
        workspace_residuals = residuals & {"workspace", "workspace_metadata"}
        if (self.child_disposal == "unconfirmed") != bool(child_residuals):
            raise ValueError("child disposal and residual obligations disagree")
        if (self.workspace_disposal == "unconfirmed") != bool(workspace_residuals):
            raise ValueError("workspace disposal and residual obligations disagree")
        if self.child_disposal == "unconfirmed" and self.workspace_disposal != "unconfirmed":
            raise ValueError("unconfirmed child disposal must retain dependent inputs")

    @property
    def confirmed(self) -> bool:
        return self.child_disposal != "unconfirmed" and self.workspace_disposal != "unconfirmed"


@dataclass(frozen=True, slots=True)
class TerminalSnapshot:
    outcome: Outcome
    cleanup: CleanupReport

    def __post_init__(self) -> None:
        if self.cleanup.child_disposal == "not_acquired" and self.outcome.child_result is not None:
            raise ValueError("child observation is incompatible with no child acquisition")
        if (
            not self.cleanup.confirmed
            and self.outcome.primary_failure is None
            and not self.outcome.diagnostics
        ):
            raise ValueError("unconfirmed disposal requires a failure diagnostic")

    def operation_failed(self, policy: CompletionPolicy) -> bool:
        return not self.cleanup.confirmed or self.outcome.operation_failed(policy)
