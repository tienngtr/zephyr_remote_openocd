# SPDX-License-Identifier: Apache-2.0

"""Remote listener specifications independent of local forwarding."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


class DuplicateEndpointError(ValueError):
    """Identify a duplicate name or port at listener and forwarding boundaries."""

    def __init__(self, subject: str):
        self.subject = subject
        super().__init__(f"{subject} must be unique")


@dataclass(frozen=True)
class ListenerSpec:
    name: str
    remote_port: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("listener name must be a non-empty string")
        port = self.remote_port
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("remote port must be in 1..65535")


def validated_listeners(listeners: Iterable[ListenerSpec]) -> tuple[ListenerSpec, ...]:
    values = tuple(listeners)
    if len({item.name for item in values}) != len(values):
        raise DuplicateEndpointError("endpoint names")
    if len({item.remote_port for item in values}) != len(values):
        raise DuplicateEndpointError("remote listener ports")
    return values
