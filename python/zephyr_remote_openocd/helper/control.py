# SPDX-License-Identifier: Apache-2.0

"""Own helper allocation, signal capture, and outer cleanup scope."""

from __future__ import annotations

import signal
from types import FrameType

from zephyr_remote_openocd.helper import _streams as streams
from zephyr_remote_openocd.helper._session import HelperSession
from zephyr_remote_openocd.helper.lifecycle import Closed
from zephyr_remote_openocd.protocol.outcome import (
    CleanupReport,
    Diagnostic,
    Outcome,
    TerminalSnapshot,
    Trigger,
)
from zephyr_remote_openocd.protocol.wire import terminal_fields


def control() -> None:
    session: HelperSession | None = None
    pending_signum: int | None = None
    previous_handlers = {}
    failure: BaseException | None = None
    cleanup_errors: list[BaseException] = []

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal pending_signum
        if pending_signum is None:
            pending_signum = signum
        if session is not None:
            session.handle_signal(signum, frame)

    try:
        # Signals report facts throughout allocation and adoption. They cannot
        # interrupt rollback or leave a successfully allocated tuple unowned.
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        session = HelperSession.create()
        if pending_signum is not None:
            session.handle_signal(pending_signum)
        session.run()
    except BaseException as exc:
        failure = exc
    finally:
        if session is not None:
            # run_async normally releases these resources. This also covers
            # failure before its cleanup scope is entered (e.g. loop setup).
            session.release_workspace()
            cleanup_errors.extend(session.cleanup_errors)
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as exc:
                cleanup_errors.append(exc)
    if session is not None:
        session.account_recorded_failures()
        if (
            isinstance(session.lifecycle.state, Closed)
            and session.lifecycle.state.local_diagnostics
            and failure is None
        ):
            failure = SystemExit(1)
    if failure is None and cleanup_errors:
        failure = cleanup_errors.pop(0)
    if failure is not None:
        for cleanup_error in cleanup_errors:
            if cleanup_error is not failure:
                note = f"session cleanup also failed: {cleanup_error}"
                if note not in getattr(failure, "__notes__", ()):
                    failure.add_note(note)
                for detail in tuple(getattr(cleanup_error, "__notes__", ())):
                    note = f"session cleanup failure detail: {detail}"
                    if note not in getattr(failure, "__notes__", ()):
                        failure.add_note(note)
        if session is None:
            diagnostic = Diagnostic.from_exception("SESSION_CREATION", failure)
            snapshot = TerminalSnapshot(
                Outcome(Trigger.HELPER_FAILURE, diagnostic),
                CleanupReport("not_acquired", "unconfirmed", ("workspace", "workspace_metadata")),
            )
            try:
                streams.emit("SESSION_ENDED", **terminal_fields(snapshot))
            finally:
                raise SystemExit(1) from failure
        # The session owns terminal delivery and bounded output cleanup; never
        # retry a blocking stdout write after the structured scope has closed.
        if session is not None and isinstance(failure, Exception):
            raise SystemExit(1) from failure
        raise failure
