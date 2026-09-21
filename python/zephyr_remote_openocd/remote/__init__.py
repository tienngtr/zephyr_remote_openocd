# SPDX-License-Identifier: Apache-2.0

"""Board-independent remote session API."""

from .backend import RemoteSession, query_remote_openocd_version
from .model import (
    RemotePathCheck,
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
    SessionState,
    StagedDirectory,
    StagedEntry,
    StagedFile,
)
from .session import SessionError

__all__ = [
    "RemotePathCheck",
    "RemoteProcess",
    "RemoteSession",
    "RemoteSessionRequest",
    "Service",
    "SessionAllocation",
    "SessionDescriptor",
    "SessionError",
    "SessionState",
    "query_remote_openocd_version",
    "StagedDirectory",
    "StagedEntry",
    "StagedFile",
]
