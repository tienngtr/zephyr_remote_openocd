# SPDX-License-Identifier: Apache-2.0

"""Board-independent remote session API."""

from zephyr_remote_openocd.protocol.process import ProcessSpec, RequiredPathSpec
from zephyr_remote_openocd.remote.model import (
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
    StagedDirectory,
    StagedEntry,
    StagedFile,
)

from .backend import RemoteSession, query_remote_openocd_version
from .forwarding import ForwardAdvisory
from .session import SessionClosedError, SessionError

__all__ = [
    "ForwardAdvisory",
    "RequiredPathSpec",
    "ProcessSpec",
    "RemoteSession",
    "RemoteSessionRequest",
    "Service",
    "SessionAllocation",
    "SessionDescriptor",
    "SessionClosedError",
    "SessionError",
    "query_remote_openocd_version",
    "StagedDirectory",
    "StagedEntry",
    "StagedFile",
]
