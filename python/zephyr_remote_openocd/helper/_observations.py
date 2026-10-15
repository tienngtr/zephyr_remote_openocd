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
    final: bool = False


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
    | SignalReceived
    | ObservationFailed
    | GroupCleaned
)
