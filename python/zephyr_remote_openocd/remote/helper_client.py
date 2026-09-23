# SPDX-License-Identifier: Apache-2.0

"""Client for a persistent remote-helper control session."""

from __future__ import annotations

import os
import selectors
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import BinaryIO, cast

from .cleanup import _add_failure_note
from .deploy import DeploymentResult
from .model import RemoteProcess, Service, SessionAllocation
from .protocol import (
    EventOrder,
    decode_message,
    read_message,
    write_start,
    write_stop,
)
from .session import SessionError, _SessionState
from .ssh import ManagedSshProcess, SshCommand, _stop_process

# The helper gives a supervised child five seconds to exit after SIGTERM,
# followed by up to four seconds joining its relay threads and workspace
# cleanup. Keep the control transport alive through that fallback and remote
# process scheduling/transport overhead.
HELPER_STOP_TIMEOUT = 15.0
HELPER_START_TIMEOUT = 10.0


@dataclass(frozen=True)
class _HelperCloseResult:
    """Helper-local shutdown failures, separated for session arbitration."""

    error: BaseException | None
    cleanup_errors: tuple[BaseException, ...]


class _HelperClient:
    """Own one helper control connection and its Protocol-v1 lifecycle."""

    def __init__(
        self,
        ssh_command: SshCommand,
        host: str,
        deployment: DeploymentResult,
        output_handler: Callable[[str, str, bool], None] | None = None,
    ) -> None:
        self._ssh_command = ssh_command
        self._host = host
        self._deployment = deployment
        self._output_handler = output_handler
        self._state = _SessionState()
        self._reader_thread: threading.Thread | None = None
        self._process: ManagedSshProcess | None = None
        self._allocation: SessionAllocation | None = None
        self._order: EventOrder | None = None

    @classmethod
    def open(
        cls,
        ssh_command: SshCommand,
        host: str,
        deployment: DeploymentResult,
        *,
        output_handler: Callable[[str, str, bool], None] | None = None,
    ) -> _HelperClient:
        client = cls(ssh_command, host, deployment, output_handler)
        client._open()
        return client

    @property
    def allocation(self) -> SessionAllocation:
        assert self._allocation is not None
        return self._allocation

    @property
    def openocd_returncode(self) -> int | None:
        return self._state.openocd_returncode

    def start_process(self, process: RemoteProcess, services: Iterable[Service]) -> str:
        service_list = tuple(services)
        helper = self._process_or_error()
        if helper.stdin is None:
            raise SessionError("helper stdin was not captured")
        write_start(cast(BinaryIO, helper.stdin), process, service_list)
        address = self._await_process_ready()
        self._start_event_drain()
        return address

    def recorded_openocd_exit(self) -> int | None:
        return self._state.recorded_openocd_exit()

    def wait_for_change(self, timeout: float | None) -> None:
        self._state.wait_for_change(timeout)

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        """Build the public wait timeout using this control process's command."""
        return subprocess.TimeoutExpired(self._process_or_error().args, timeout)

    def close(self) -> _HelperCloseResult:
        """Stop the helper without deciding whole-session failure precedence."""
        helper = self._process_or_error()
        logical_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []

        terminal_before_stop = self._state.terminal_reason
        helper_status = helper.poll()
        if terminal_before_stop == "requested":
            logical_error = SessionError(
                "helper reported SESSION_CLOSED(reason='requested') before STOP"
            )
        elif helper_status is None and terminal_before_stop is None:
            if self._reader_thread is None:
                self._start_event_drain()
            if helper.stdin is None:
                logical_error = SessionError("helper stdin was not captured")
            else:

                def write_requested_stop() -> None:
                    write_stop(cast(BinaryIO, helper.stdin))

                try:
                    terminal_before_stop = self._state.request_stop(write_requested_stop)
                except BaseException as error:
                    logical_error = error
                else:
                    if terminal_before_stop == "requested":
                        logical_error = SessionError(
                            "helper reported SESSION_CLOSED(reason='requested') before STOP"
                        )
                try:
                    helper.stdin.close()
                except BaseException as error:
                    cleanup_errors.append(error)
                if logical_error is None:
                    try:
                        helper.wait(timeout=HELPER_STOP_TIMEOUT)
                    except BaseException as error:
                        logical_error = error

        reader_stopped = self._join_reader()
        try:
            _stop_process(helper, close_streams=reader_stopped)
        except BaseException as error:
            cleanup_errors.append(error)

        if self._reader_thread is not None:
            initially_stopped = reader_stopped
            reader_stopped = self._join_reader()
            if reader_stopped and not initially_stopped:
                try:
                    _stop_process(helper, close_streams=True)
                except BaseException as error:
                    cleanup_errors.append(error)
            if not reader_stopped:
                cleanup_errors.append(SessionError("helper event reader did not stop"))

        self._emit_diagnostic()

        reader_eof = self._state.reader_failure_has_eof_cause()
        reader_failure = self._state.reader_failure()
        if reader_failure is not None and not reader_eof:
            logical_error = logical_error or reader_failure

        terminal = self._state.terminal_reason
        if logical_error is None:
            helper_status = helper.poll()
            if terminal not in {"requested", "process_exit"}:
                logical_error = SessionError(
                    "helper shutdown did not produce "
                    "SESSION_CLOSED(reason='requested' or 'process_exit') "
                    f"(terminal={terminal!r}, exit={helper_status!r})"
                )
            elif helper_status not in (0, None):
                logical_error = SessionError(
                    f"remote helper exited with status {helper_status} after {terminal} shutdown"
                )
        if logical_error is None and reader_failure is not None:
            logical_error = reader_failure

        if logical_error is not None:
            for cleanup_error in cleanup_errors:
                _add_failure_note(logical_error, "helper cleanup also failed", cleanup_error)
        return _HelperCloseResult(logical_error, tuple(cleanup_errors))

    def _open(self) -> None:
        command = f"python3 {shlex.quote(self._deployment.path)} control"
        self._process = self._ssh_command.popen(self._host, command)
        try:
            if self._process.stdout is None:
                raise SessionError("helper stdout was not captured")
            self._order = EventOrder()
            created = self._read_event(time.monotonic() + HELPER_START_TIMEOUT)
            self._allocation = SessionAllocation(created["session_id"], created["remote_workspace"])
        except BaseException as error:
            try:
                _stop_process(self._process)
            except BaseException as cleanup_error:
                _add_failure_note(error, "helper startup cleanup also failed", cleanup_error)
            raise

    def _read_event(self, deadline: float | None = None) -> dict:
        helper = self._process_or_error()
        stream = cast(BinaryIO, helper.stdout)
        try:
            message = (
                read_message(stream)
                if deadline is None
                else self._read_initial_message(stream, deadline)
            )
        except EOFError as error:
            diagnostic = helper.stderr_tail()
            raise SessionError(
                "remote helper terminated: " + diagnostic.decode("utf-8", "replace")
            ) from error
        except TimeoutError as error:
            diagnostic = helper.stderr_tail()
            suffix = ": " + diagnostic.decode("utf-8", "replace").strip() if diagnostic else ""
            raise SessionError("remote helper startup timed out" + suffix) from error
        assert self._order is not None
        self._order.accept(message)
        if message["type"] == "ERROR":
            raise SessionError(f"remote helper error: {message.get('message', 'unknown error')}")
        return message

    @staticmethod
    def _read_initial_message(stream: BinaryIO, deadline: float) -> dict:
        descriptor = stream.fileno()
        pending = bytearray()
        selector = selectors.DefaultSelector()
        try:
            selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("remote helper startup timed out")
                if not selector.select(remaining):
                    continue
                chunk = os.read(descriptor, 1)
                if not chunk:
                    if pending:
                        return decode_message(bytes(pending))
                    raise EOFError("helper control channel closed")
                pending.extend(chunk)
                if chunk == b"\n":
                    return decode_message(bytes(pending))
        finally:
            selector.close()

    def _await_process_ready(self) -> str:
        while True:
            event = self._read_event()
            self._dispatch(event)
            if event["type"] == "PROCESS_READY":
                return event["remote_address"]
            if event["type"] == "SESSION_CLOSED":
                raise SessionError(
                    f"remote process closed before becoming ready ({event.get('returncode')})"
                )

    def _start_event_drain(self) -> None:
        if self._reader_thread is not None:
            return
        self._reader_thread = threading.Thread(target=self._drain_events, daemon=True)
        self._reader_thread.start()

    def _dispatch(self, event: dict) -> None:
        if event["type"] == "CHILD_OUTPUT" and self._output_handler is not None:
            self._output_handler(event["stream"], event["payload"], event["line_end"])
        elif event["type"] == "SESSION_CLOSED":
            self._state.record_terminal(event["reason"], event["returncode"])

    def _drain_events(self) -> None:
        try:
            while True:
                self._dispatch(self._read_event())
        except BaseException as error:
            terminal_reason = self._state.terminal_reason
            helper = self._process_or_error()
            if isinstance(error.__cause__, EOFError):
                helper_status = helper.poll()
                if terminal_reason is not None and helper_status in (0, None):
                    return
                if terminal_reason is None:
                    detail = (
                        "remote helper exited without a terminal event"
                        if helper_status is None
                        else "remote helper exited with status "
                        f"{helper_status} without a terminal event"
                    )
                else:
                    detail = (
                        f"remote helper exited with status {helper_status} "
                        f"after {terminal_reason} shutdown"
                    )
                failure = SessionError(detail)
                failure.__cause__ = error.__cause__
                error = failure
            self._state.record_reader_failure(error)

    def _join_reader(self, timeout: float = 2.0) -> bool:
        if self._reader_thread is None:
            return True
        self._reader_thread.join(timeout=timeout)
        return not self._reader_thread.is_alive()

    def _emit_diagnostic(self) -> None:
        if self._output_handler is None:
            return
        try:
            diagnostic = self._process_or_error().stderr_tail()
            if not diagnostic:
                return
            fragments = diagnostic.decode("utf-8", "replace").split("\n")
            for index, fragment in enumerate(fragments):
                line_end = index < len(fragments) - 1
                if fragment or line_end:
                    self._output_handler("stderr", fragment, line_end)
        except BaseException:
            return

    def _process_or_error(self) -> ManagedSshProcess:
        if self._process is None:
            raise SessionError("helper control session is not open")
        return self._process
