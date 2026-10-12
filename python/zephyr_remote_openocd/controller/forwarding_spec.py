# SPDX-License-Identifier: Apache-2.0

"""Immutable requests for local SSH forwards to remote listeners."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from zephyr_remote_openocd.protocol.listeners import DuplicateEndpointError, ListenerSpec


@dataclass(frozen=True)
class ForwardSpec:
    name: str
    local_port: int
    remote_port: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("forward name must be a non-empty string")
        for label, port in (("local", self.local_port), ("remote", self.remote_port)):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError(f"{label} port must be in 1..65535")

    @property
    def listener(self) -> ListenerSpec:
        return ListenerSpec(self.name, self.remote_port)


def _ensure_unique(values, label: str) -> None:
    values = tuple(values)
    if len(values) != len(set(values)):
        raise DuplicateEndpointError(label)


def validated_forwards(forwards: Iterable[ForwardSpec]) -> tuple[ForwardSpec, ...]:
    """Freeze a forwarding collection after validating its set-wide invariants."""

    values = tuple(forwards)
    _ensure_unique((item.name for item in values), "endpoint names")
    _ensure_unique((item.local_port for item in values), "local forwarding ports")
    _ensure_unique((item.remote_port for item in values), "remote listener ports")
    return values
