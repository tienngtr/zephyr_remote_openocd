# SPDX-License-Identifier: Apache-2.0

"""Validated immutable process and required-path specifications."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from zephyr_remote_openocd.protocol.arguments import (
    ArgumentTemplate,
    TclWord,
    log_spool_requested,
)
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy

_EVIDENCE_NAME = re.compile(r"[A-Za-z0-9_]+")
# Helper-owned readiness artifact below the session workspace (Protocol v1).
READINESS_ARTIFACT = "readiness"


def valid_evidence_name(name: object) -> bool:
    """A readiness evidence name is a non-empty run of ASCII letters, digits, and underscores."""
    return isinstance(name, str) and _EVIDENCE_NAME.fullmatch(name) is not None


@dataclass(frozen=True)
class RequiredPathSpec:
    path: str
    kind: Literal["file", "directory"]
    template: ArgumentTemplate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or "\0" in self.path:
            raise ValueError("remote path check must have a non-empty path")
        if self.kind not in {"file", "directory"}:
            raise ValueError("remote path check kind is invalid")
        if self.template is not None and any(
            isinstance(part, TclWord) for part in self.template.parts
        ):
            raise ValueError("remote path checks cannot contain Tcl words")
        if self.template is not None and not self.template.preview():
            raise ValueError("remote path check template must not be empty")


@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    required_paths: tuple[RequiredPathSpec, ...] = field(default_factory=tuple)
    readiness_evidence: tuple[str, ...] = field(default_factory=tuple)
    readiness_timeout: float = 30.0
    literal_prefix: int = 0
    argv_templates: tuple[tuple[int, ArgumentTemplate], ...] = ()
    # Explicit operation policy shared with the helper request.
    completion_policy: CompletionPolicy = CompletionPolicy.LIVE_SERVER

    def __post_init__(self) -> None:
        object.__setattr__(self, "completion_policy", CompletionPolicy(self.completion_policy))
        argv, environment, required_paths, readiness_evidence = _normalized_process_fields(self)
        _validate_process_argv(argv)
        _validate_process_environment(environment)
        _validate_process_paths(required_paths)
        _validate_process_readiness(readiness_evidence, self.readiness_timeout)
        _validate_literal_prefix(self.literal_prefix, len(argv))
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "required_paths", required_paths)
        object.__setattr__(self, "readiness_evidence", readiness_evidence)
        templates = tuple(self.argv_templates)
        indices = [index for index, _ in templates]
        if len(indices) != len(set(indices)) or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or not self.literal_prefix <= index < len(argv)
            or not isinstance(template, ArgumentTemplate)
            for index, template in templates
        ):
            raise ValueError("argv templates must target unique non-prefix argument indices")
        object.__setattr__(self, "argv_templates", templates)
        log_spool_requested(
            (template for _, template in templates),
            (check.template for check in required_paths if check.template is not None),
        )

    @property
    def uses_log_spool(self) -> bool:
        """Whether argv requests the session log channel; invalid placements raise."""
        return log_spool_requested(
            (template for _, template in self.argv_templates),
            (check.template for check in self.required_paths if check.template is not None),
        )


def _normalized_process_fields(
    process: ProcessSpec,
) -> tuple[
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[RequiredPathSpec, ...],
    tuple[str, ...],
]:
    """Freeze collection-valued process fields before validating them."""

    raw_evidence: Any = process.readiness_evidence
    if isinstance(raw_evidence, (str, bytes)):
        raise ValueError("readiness evidence must be a collection of names")

    return (
        tuple(process.argv),
        tuple(process.environment),
        tuple(process.required_paths),
        tuple(raw_evidence),
    )


def _validate_process_argv(argv: tuple[str, ...]) -> None:
    if (
        not argv
        or not isinstance(argv[0], str)
        or not argv[0]
        or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
    ):
        raise ValueError("remote process argv must start with a non-empty string")


def _validate_process_environment(environment: tuple[tuple[str, str], ...]) -> None:
    names = [name for name, _ in environment]
    if len(names) != len(set(names)):
        raise ValueError("remote environment names must be unique and valid")
    for name in names:
        if not isinstance(name, str) or not name or "=" in name or "\0" in name:
            raise ValueError("remote environment names must be unique and valid")
    for _, value in environment:
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("remote environment values must be strings without NUL")


def _validate_process_paths(required_paths: tuple[RequiredPathSpec, ...]) -> None:
    if not all(
        isinstance(check, RequiredPathSpec)
        and check.path
        and "\0" not in check.path
        and check.kind in {"file", "directory"}
        for check in required_paths
    ):
        raise ValueError("remote path checks must be RequiredPathSpec values")


def _validate_process_readiness(readiness_evidence: tuple[str, ...], timeout: float) -> None:
    if not all(valid_evidence_name(name) for name in readiness_evidence) or len(
        readiness_evidence
    ) != len(set(readiness_evidence)):
        raise ValueError("readiness evidence must be unique names of ASCII letters, digits, or _")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("readiness timeout must be positive")
    try:
        seconds = float(timeout)
    except OverflowError as error:
        # Mirror START decoding: the timeout domain is finite binary64.
        raise ValueError("readiness timeout does not convert to a finite float") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("readiness timeout must be positive")


def _validate_literal_prefix(literal_prefix: int, argv_length: int) -> None:
    if (
        isinstance(literal_prefix, bool)
        or not isinstance(literal_prefix, int)
        or not 0 <= literal_prefix <= argv_length
    ):
        raise ValueError("literal argv prefix is invalid")
