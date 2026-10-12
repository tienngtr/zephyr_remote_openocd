# SPDX-License-Identifier: Apache-2.0

"""Board-independent descriptions of a remote debugging session."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from zephyr_remote_openocd.controller.ssh import SshCommand
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.process import ProcessSpec


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

    @property
    def listener(self) -> ListenerSpec:
        return ListenerSpec(self.name, self.remote_port)


@dataclass(frozen=True)
class SessionPlan:
    host: str
    ssh_command: SshCommand
    process: ProcessSpec
    staged_files: tuple[StagedEntry, ...] = field(default_factory=tuple)
    services: tuple[Service, ...] = field(default_factory=tuple)
    auxiliary_services: tuple[Service, ...] = field(default_factory=tuple)
    reserved_services: tuple[Service, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("remote host must not be empty")
        object.__setattr__(self, "staged_files", tuple(self.staged_files))
        services = validated_services(self.services)
        reserved = validated_services(self.reserved_services)
        validated_services((*services, *reserved))
        object.__setattr__(self, "services", services)
        auxiliary = validated_services(self.auxiliary_services)
        if not set(auxiliary).issubset(services):
            raise ValueError("best-effort services must belong to the initial service set")
        object.__setattr__(self, "auxiliary_services", auxiliary)
        object.__setattr__(self, "reserved_services", reserved)

    @property
    def address_services(self) -> tuple[Service, ...]:
        """Services the helper validates while allocating the session address."""

        return (*self.services, *self.reserved_services)


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
