# SPDX-License-Identifier: Apache-2.0

"""Immutable controller session plans and allocated session descriptions."""

from __future__ import annotations

from dataclasses import dataclass, field

from zephyr_remote_openocd.controller._ssh import SshCommand
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec, validated_forwards
from zephyr_remote_openocd.controller.staging_spec import StagedEntry
from zephyr_remote_openocd.protocol.listeners import ListenerSpec, validated_listeners
from zephyr_remote_openocd.protocol.process import ProcessSpec


@dataclass(frozen=True)
class SessionPlan:
    host: str
    ssh_command: SshCommand
    process: ProcessSpec
    staged_files: tuple[StagedEntry, ...] = field(default_factory=tuple)
    forwards: tuple[ForwardSpec, ...] = field(default_factory=tuple)
    auxiliary_forwards: tuple[ForwardSpec, ...] = field(default_factory=tuple)
    reserved_listeners: tuple[ListenerSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("remote host must not be empty")
        object.__setattr__(self, "staged_files", tuple(self.staged_files))
        forwards = validated_forwards(self.forwards)
        reserved = validated_listeners(self.reserved_listeners)
        validated_listeners((*(forward.listener for forward in forwards), *reserved))
        object.__setattr__(self, "forwards", forwards)
        auxiliary = validated_forwards(self.auxiliary_forwards)
        if not set(auxiliary).issubset(forwards):
            raise ValueError("best-effort forwards must belong to the initial forwarding set")
        object.__setattr__(self, "auxiliary_forwards", auxiliary)
        object.__setattr__(self, "reserved_listeners", reserved)

    @property
    def listeners(self) -> tuple[ListenerSpec, ...]:
        """Remote listeners the helper validates while allocating the session address."""

        return (*(forward.listener for forward in self.forwards), *self.reserved_listeners)


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
