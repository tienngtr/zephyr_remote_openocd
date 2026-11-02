# SPDX-License-Identifier: Apache-2.0

"""Immutable physical facts delivered to the helper coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from zephyr_remote_openocd.protocol.commands import StartRequest

if TYPE_CHECKING:
    from zephyr_remote_openocd.helper._process import SupervisedProcess


class StartReceived(NamedTuple):
    request: StartRequest


class ControlEOF(NamedTuple):
    pass


class ChildOutput(NamedTuple):
    child: SupervisedProcess
    stream: str
    chunk: bytes


class ChildExited(NamedTuple):
    child: SupervisedProcess
    returncode: int


class Deadline(NamedTuple):
    child: SupervisedProcess
    readiness: bool


class ReadinessPoll(NamedTuple):
    """A tick on which the coordinator itself reads the readiness artifact."""

    child: SupervisedProcess


class LogChunk(NamedTuple):
    """Bytes read from the log channel; empty at its final end-of-file."""

    chunk: bytes


class LogCredit(NamedTuple):
    """The writer completed LOG_DATA; repeated returns coalesce in one latch."""


class LogReaderStopped(NamedTuple):
    """The log reader ended after recording a deferred read failure."""


class LogDeadline(NamedTuple):
    """The final log drain reached its absolute deadline."""


class SignalReceived(NamedTuple):
    signum: int


class ObservationFailed(NamedTuple):
    source: str
    exception: BaseException
    child: SupervisedProcess | None = None


class GroupCleaned(NamedTuple):
    child: SupervisedProcess
    exception: BaseException | None


Observation = (
    StartReceived
    | ControlEOF
    | ChildOutput
    | ChildExited
    | Deadline
    | ReadinessPoll
    | LogChunk
    | LogCredit
    | LogReaderStopped
    | LogDeadline
    | SignalReceived
    | ObservationFailed
    | GroupCleaned
)
