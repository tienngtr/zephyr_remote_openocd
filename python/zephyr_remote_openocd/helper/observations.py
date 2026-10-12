# SPDX-License-Identifier: Apache-2.0

"""Immutable physical facts delivered to the helper coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from zephyr_remote_openocd.protocol.commands import StartRequest

if TYPE_CHECKING:
    from zephyr_remote_openocd.helper.process import SupervisedProcess


class _StartReceived(NamedTuple):
    request: StartRequest


class _ControlEOF(NamedTuple):
    pass


class _ChildOutput(NamedTuple):
    child: SupervisedProcess
    stream: str
    chunk: bytes


class _ChildExited(NamedTuple):
    child: SupervisedProcess
    returncode: int


class _Deadline(NamedTuple):
    child: SupervisedProcess
    readiness: bool
    final: bool = False


class _SignalReceived(NamedTuple):
    signum: int


class _ObservationFailed(NamedTuple):
    source: str
    exception: BaseException
    child: SupervisedProcess | None = None


class _GroupCleaned(NamedTuple):
    child: SupervisedProcess
    exception: BaseException | None


_Observation = (
    _StartReceived
    | _ControlEOF
    | _ChildOutput
    | _ChildExited
    | _Deadline
    | _SignalReceived
    | _ObservationFailed
    | _GroupCleaned
)
