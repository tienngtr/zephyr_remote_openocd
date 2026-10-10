# SPDX-License-Identifier: Apache-2.0

"""Construct complete terminal frames at controlled helper transport boundaries."""

from zephyr_remote_openocd.remote.outcome import (
    ChildResult,
    CleanupReport,
    Diagnostic,
    Outcome,
    TerminalSnapshot,
    Trigger,
)
from zephyr_remote_openocd.remote.protocol import encode_message
from zephyr_remote_openocd.remote.wire import terminal_fields


def terminal_snapshot(
    trigger: Trigger = Trigger.CONTROLLER_EOF,
    *,
    returncode: int | None = None,
    generation: int = 1,
    termination_requested: bool = False,
    code: str | None = None,
    message: str = "controlled failure",
) -> TerminalSnapshot:
    result = (
        None if returncode is None else ChildResult(generation, returncode, termination_requested)
    )
    return TerminalSnapshot(
        Outcome(trigger, None if code is None else Diagnostic(code, message), child_result=result),
        CleanupReport("not_acquired" if result is None else "confirmed", "confirmed"),
    )


def terminal_message(*args, **kwargs) -> bytes:
    return encode_message("SESSION_ENDED", **terminal_fields(terminal_snapshot(*args, **kwargs)))
