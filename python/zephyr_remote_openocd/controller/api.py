# SPDX-License-Identifier: Apache-2.0

"""Canonical package-internal vocabulary for constructing and running sessions.

This is a maintainer convenience, not a supported external Python API. Import
specific subsystems directly when implementing controller collaboration or pure
offline planning; they must not depend back on this runtime entry point.
Package initialization stays import-free for lightweight and deployed callers.
"""

from zephyr_remote_openocd.controller._forwarding import ForwardAdvisory
from zephyr_remote_openocd.controller._ssh import SshCommand
from zephyr_remote_openocd.controller.authority import SessionClosedError, SessionError
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.plan import SessionAllocation, SessionDescriptor, SessionPlan
from zephyr_remote_openocd.controller.session import SessionController, query_remote_openocd_version
from zephyr_remote_openocd.controller.staging_spec import (
    StagedDirectory,
    StagedEntry,
    StagedFile,
)

__all__ = [
    "ForwardAdvisory",
    "ForwardSpec",
    "SessionAllocation",
    "SessionClosedError",
    "SessionController",
    "SessionDescriptor",
    "SessionError",
    "SessionPlan",
    "SshCommand",
    "StagedDirectory",
    "StagedEntry",
    "StagedFile",
    "query_remote_openocd_version",
]
