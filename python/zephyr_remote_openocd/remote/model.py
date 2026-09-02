"""Board-independent descriptions of a remote debugging session."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path, PurePosixPath

from .ssh import SshCommand


class SessionState(Enum):
    NEW = auto()
    CREATED = auto()
    STAGED = auto()
    STARTING = auto()
    READY = auto()
    STOPPING = auto()
    CLOSED = auto()
    FAILED = auto()


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
class Service:
    name: str
    local_port: int
    remote_port: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("service name must not be empty")
        for label, port in (("local", self.local_port), ("remote", self.remote_port)):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError(f"{label} port must be in 1..65535")


@dataclass(frozen=True)
class RemoteSessionRequest:
    host: str
    ssh_command: SshCommand
    staged_files: tuple[StagedFile, ...] = field(default_factory=tuple)
    services: tuple[Service, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("remote host must not be empty")
        object.__setattr__(self, "staged_files", tuple(self.staged_files))
        object.__setattr__(self, "services", tuple(self.services))
        names = [item.name for item in self.services]
        if len(names) != len(set(names)):
            raise ValueError("service names must be unique")
        local = [item.local_port for item in self.services]
        if len(local) != len(set(local)):
            raise ValueError("local service ports must be unique")


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
