# SPDX-License-Identifier: Apache-2.0

"""Board-independent descriptions of a remote debugging session."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path, PurePosixPath
from typing import Literal

from .ssh import SshCommand


class SessionState(Enum):
    NEW = auto()
    READY = auto()
    CLOSED = auto()
    FAILED = auto()


class DuplicateServiceError(ValueError):
    """Identify the duplicated service attribute for boundary diagnostics."""

    def __init__(self, subject: str):
        self.subject = subject
        super().__init__(f"{subject} must be unique")


def validated_destination(value: str | PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(value)
    if not str(path) or str(path) == "." or path.is_absolute():
        raise ValueError(f"staged destination must be a non-empty relative path: {value!s}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"staged destination is not normalized: {value!s}")
    return path


@dataclass(frozen=True)
class StagedFile:
    source: Path
    destination: PurePosixPath

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Path(self.source))
        object.__setattr__(self, "destination", validated_destination(self.destination))


@dataclass(frozen=True)
class StagedDirectory:
    source: Path
    destination: PurePosixPath

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Path(self.source))
        object.__setattr__(self, "destination", validated_destination(self.destination))


StagedEntry = StagedFile | StagedDirectory


@dataclass(frozen=True)
class Service:
    name: str
    local_port: int
    remote_port: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("service name must be a non-empty string")
        for label, port in (("local", self.local_port), ("remote", self.remote_port)):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError(f"{label} port must be in 1..65535")


@dataclass(frozen=True)
class RemoteSessionRequest:
    host: str
    ssh_command: SshCommand
    process: RemoteProcess
    staged_files: tuple[StagedEntry, ...] = field(default_factory=tuple)
    services: tuple[Service, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("remote host must not be empty")
        object.__setattr__(self, "staged_files", tuple(self.staged_files))
        object.__setattr__(self, "services", validated_services(self.services))


@dataclass(frozen=True)
class SessionAllocation:
    session_id: str
    remote_workspace: str


@dataclass(frozen=True)
class SessionDescriptor:
    allocation: SessionAllocation
    remote_address: str

    @property
    def session_id(self) -> str:
        return self.allocation.session_id

    @property
    def remote_workspace(self) -> str:
        return self.allocation.remote_workspace


@dataclass(frozen=True)
class RemotePathCheck:
    path: str
    kind: Literal["file", "directory"]

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path or "\0" in self.path:
            raise ValueError("remote path check must have a non-empty path")
        if self.kind not in {"file", "directory"}:
            raise ValueError("remote path check kind is invalid")


@dataclass(frozen=True)
class RemoteProcess:
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    required_paths: tuple[RemotePathCheck, ...] = field(default_factory=tuple)
    readiness_marker: str | None = None
    readiness_timeout: float = 30.0
    literal_prefix: int = 0

    def __post_init__(self) -> None:
        argv, environment, required_paths = _normalized_process_fields(self)
        _validate_process_argv(argv)
        _validate_process_environment(environment)
        _validate_process_paths(required_paths)
        _validate_process_readiness(self.readiness_marker, self.readiness_timeout)
        _validate_literal_prefix(self.literal_prefix, len(argv))
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "required_paths", required_paths)


def _normalized_process_fields(
    process: RemoteProcess,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[RemotePathCheck, ...]]:
    """Freeze collection-valued process fields before validating them."""

    return tuple(process.argv), tuple(process.environment), tuple(process.required_paths)


def _validate_process_argv(argv: tuple[str, ...]) -> None:
    if (
        not argv
        or not isinstance(argv[0], str)
        or not argv[0]
        or not all(isinstance(arg, str) for arg in argv[1:])
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


def _validate_process_paths(required_paths: tuple[RemotePathCheck, ...]) -> None:
    if not all(
        isinstance(check, RemotePathCheck)
        and check.path
        and "\0" not in check.path
        and check.kind in {"file", "directory"}
        for check in required_paths
    ):
        raise ValueError("remote path checks must be RemotePathCheck values")


def _validate_process_readiness(marker: str | None, timeout: float) -> None:
    if marker is not None and not isinstance(marker, str):
        raise ValueError("readiness marker must be a non-empty token")
    if marker is not None and (not marker or any(character.isspace() for character in marker)):
        raise ValueError("readiness marker must be a non-empty token")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("readiness timeout must be positive")


def _validate_literal_prefix(literal_prefix: int, argv_length: int) -> None:
    if (
        isinstance(literal_prefix, bool)
        or not isinstance(literal_prefix, int)
        or not 0 <= literal_prefix <= argv_length
    ):
        raise ValueError("literal argv prefix is invalid")


def _ensure_unique(values, label: str) -> None:
    values = tuple(values)
    if len(values) != len(set(values)):
        raise DuplicateServiceError(label)


def validated_services(services: Iterable[Service]) -> tuple[Service, ...]:
    """Freeze a service collection after validating its set-wide invariants."""

    values = tuple(services)
    _ensure_unique((item.name for item in values), "service names")
    _ensure_unique((item.local_port for item in values), "local service ports")
    _ensure_unique((item.remote_port for item in values), "remote service ports")
    return values
