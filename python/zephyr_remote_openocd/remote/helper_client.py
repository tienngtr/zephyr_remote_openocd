# SPDX-License-Identifier: Apache-2.0

"""Client for a remote-helper session control channel."""

from __future__ import annotations

import os
import selectors
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import BinaryIO, cast

from .cleanup import _add_failure_note
from .deploy import DeploymentResult
from .model import RemoteProcess, Service, SessionAllocation
from .outcome import CompletionPolicy
from .protocol import (
    EventOrder,
    ProtocolError,
    decode_single_frame,
    read_message,
    write_start,
)
from .session import SessionError, _SessionObservations
from .ssh import ManagedSshProcess, SshCommand, _stop_process
from .wire import MAX_FRAME_SIZE, decode_terminal

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


@dataclass(frozen=True)
class _ShutdownAttempt:
    """Outcome of requesting graceful helper shutdown."""

    error: BaseException | None
    cleanup_errors: tuple[BaseException, ...]


class _HelperClient:
    """Own one helper control connection and its controller-lifetime lease."""

    def __init__(
        self,
        ssh_command: SshCommand,
        host: str,
        deployment: DeploymentResult,
        output_handler: Callable[[str, str, bool], None] | None = None,
        *,
        process_start_handler: Callable[[tuple[str, ...]], None] | None = None,
        observations: _SessionObservations | None = None,
    ) -> None:
        self._ssh_command = ssh_command
        self._host = host
        self._deployment = deployment
        self._output_handler = output_handler
        self._process_start_handler = process_start_handler
        self._observations = observations if observations is not None else _SessionObservations()
        self._reader_thread: threading.Thread | None = None
        self._process: ManagedSshProcess | None = None
        self._allocation: SessionAllocation | None = None
        self._order: EventOrder | None = None

    @property
    def allocation(self) -> SessionAllocation:
        assert self._allocation is not None
        return self._allocation

    @property
    def openocd_returncode(self) -> int | None:
        ending = self._observations.snapshot().ending
        result = None if ending is None else ending.outcome.child_result
        return None if result is None else result.returncode

    def start_process(
        self,
        process: RemoteProcess,
        services: Iterable[Service],
        *,
        preferred_address: str | None = None,
    ) -> str | None:
        self._observations.set_policy(process.completion_policy)
        assert self._order is not None
        self._order.policy = process.completion_policy
        service_list = tuple(services)
        helper = self._process_or_error()
        if helper.stdin is None:
            raise SessionError("helper stdin was not captured")
        write_start(
            cast(BinaryIO, helper.stdin), process, service_list, preferred_address=preferred_address
        )
        address = (
            self._await_process_ready()
            if process.completion_policy == CompletionPolicy.LIVE_SERVER
            else None
        )
        self._start_event_drain()
        return address

    def recorded_openocd_exit(self) -> int | None:
        helper_error = self._observations.helper_error_for_operation()
        if helper_error is not None:
            raise helper_error
        if self._process is not None:
            status = self._process.poll()
            if status not in (None, 0):
                raise SessionError(f"remote helper/SSH exited with status {status}")
        return self.openocd_returncode

    def wait_for_change(self, timeout: float | None) -> None:
        self._observations.wait_for_change(timeout)

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        """Build the public wait timeout using this control process's command."""
        return subprocess.TimeoutExpired(self._process_or_error().args, timeout)

    def close(self) -> _HelperCloseResult:
        """Stop the helper without choosing the whole-session primary failure."""
        if self._process is None:
            return _HelperCloseResult(None, ())
        helper = self._process_or_error()
        logical_error: BaseException | None
        try:
            logical_error = self._observations.take_unreported_helper_error()
        except BaseException as error:
            logical_error = error
        shutdown = self._request_shutdown(helper)
        if shutdown.error is not None:
            if logical_error is None:
                logical_error = shutdown.error
            else:
                _add_failure_note(logical_error, "helper shutdown also failed", shutdown.error)
        cleanup_errors = list(shutdown.cleanup_errors)
        cleanup_errors.extend(self._cleanup_control_process(helper))
        self._emit_diagnostic()
        try:
            logical_error = self._resolve_shutdown_result(helper, logical_error)
        except BaseException as error:
            if logical_error is None:
                logical_error = error
            else:
                _add_failure_note(logical_error, "helper status also failed", error)

        if logical_error is not None:
            for cleanup_error in cleanup_errors:
                _add_failure_note(logical_error, "helper cleanup also failed", cleanup_error)
        return _HelperCloseResult(logical_error, tuple(cleanup_errors))

    def _request_shutdown(self, helper: ManagedSshProcess) -> _ShutdownAttempt:
        """Half-close stdin while retaining reverse output for bounded shutdown."""
        self._observations.cancel()
        errors: list[BaseException] = []
        logical_error = None
        try:
            if self._reader_thread is None:
                self._start_event_drain()
        except BaseException as error:
            logical_error = error
        try:
            if helper.stdin is not None:
                helper.stdin.close()
        except BaseException as error:
            if logical_error is None:
                logical_error = error
            else:
                errors.append(error)
        try:
            helper.poll()
        except BaseException as error:
            if logical_error is None:
                logical_error = error
            else:
                errors.append(error)
        try:
            helper.wait(timeout=HELPER_STOP_TIMEOUT)
        except BaseException as error:
            if logical_error is None:
                logical_error = error
            else:
                errors.append(error)
        return _ShutdownAttempt(logical_error, tuple(errors))

    def _cleanup_control_process(self, helper: ManagedSshProcess) -> tuple[BaseException, ...]:
        """Stop the reader and helper process without abandoning cleanup."""
        cleanup_errors: list[BaseException] = []
        try:
            reader_stopped = self._join_reader()
        except BaseException as error:
            cleanup_errors.append(error)
            reader_stopped = False
        try:
            _stop_process(helper, close_streams=reader_stopped)
        except BaseException as error:
            cleanup_errors.append(error)

        if self._reader_thread is not None:
            initially_stopped = reader_stopped
            try:
                reader_stopped = self._join_reader()
            except BaseException as error:
                cleanup_errors.append(error)
                reader_stopped = False
            if reader_stopped and not initially_stopped:
                try:
                    _stop_process(helper, close_streams=True)
                except BaseException as error:
                    cleanup_errors.append(error)
            if not reader_stopped:
                cleanup_errors.append(SessionError("helper event reader did not stop"))
        return tuple(cleanup_errors)

    def _resolve_shutdown_result(
        self,
        helper: ManagedSshProcess,
        logical_error: BaseException | None,
    ) -> BaseException | None:
        """Reconcile final helper observations into one logical failure."""
        errors = []
        terminal_error = self._observations.take_unreported_helper_error()
        if terminal_error is not None:
            errors.append(terminal_error)
        snapshot = self._observations.snapshot()
        if snapshot.ending is None:
            errors.append(SessionError("helper shutdown did not produce SESSION_ENDED"))
        helper_status = helper.poll()
        if helper_status not in (0, None):
            errors.append(SessionError(f"remote helper/SSH exited with status {helper_status}"))
        for error in errors:
            if logical_error is None:
                logical_error = error
            else:
                _add_failure_note(logical_error, "helper shutdown also failed", error)
        return logical_error

    def acquire(self) -> None:
        """Acquire control resources; the owning caller must close on failure."""
        command = f"python3 {shlex.quote(self._deployment.path)} control"
        # Mask only launch and adoption, not authentication/session readiness.
        # SshCommand.popen() preserves this mask until the returned transport
        # has been stored on the already-owned helper client.
        self._order = EventOrder()
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            self._process = self._ssh_command.popen(self._host, command)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if self._process.stdout is None:
            raise SessionError("helper stdout was not captured")
        created = self._read_event(time.monotonic() + HELPER_START_TIMEOUT)
        if created["type"] == "SESSION_ENDED":
            self._dispatch(created)
            error = self._observations.helper_error_for_operation()
            raise error or SessionError("helper ended before session creation")
        self._allocation = SessionAllocation(created["session_id"], created["remote_workspace"])

    def _read_event(self, deadline: float | None = None, *, report_error: bool = True) -> dict:
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
                        raise ProtocolError("protocol frame is missing its LF delimiter")
                    raise EOFError("helper control channel closed")
                pending.extend(chunk)
                if len(pending) > MAX_FRAME_SIZE:
                    raise ProtocolError("protocol frame exceeds the size limit")
                if chunk == b"\n":
                    return decode_single_frame(bytes(pending))
        finally:
            selector.close()

    def _await_process_ready(self) -> str:
        while True:
            event = self._read_event()
            self._dispatch(event)
            if event["type"] == "READY":
                return event["remote_address"]
            if event["type"] == "SESSION_ENDED":
                error = self._observations.helper_error_for_operation()
                raise error or SessionError("remote process ended before READY")

    def _start_event_drain(self) -> None:
        if self._reader_thread is not None:
            return
        reader_thread = threading.Thread(target=self._drain_events, daemon=True)
        # Adopt the started reader before pending SIGINT can escape to cleanup.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            reader_thread.start()
            self._reader_thread = reader_thread
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def _dispatch(self, event: dict) -> None:
        if event["type"] == "ATTEMPT" and self._process_start_handler is not None:
            self._process_start_handler(tuple(event["argv"]))
        elif event["type"] == "CHILD_OUTPUT" and self._output_handler is not None:
            self._output_handler(event["stream"], event["payload"], event["line_end"])
        elif event["type"] == "READY":
            self._observations.record_ready()
        elif event["type"] == "SESSION_ENDED":
            self._observations.record_terminal(decode_terminal(event))

    def _drain_events(self) -> None:
        try:
            while True:
                self._dispatch(self._read_event(report_error=False))
        except BaseException as error:
            snapshot = self._observations.snapshot()
            if isinstance(error.__cause__, EOFError) and snapshot.ending is not None:
                return
            self._observations.record_reader_failure(error)

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
