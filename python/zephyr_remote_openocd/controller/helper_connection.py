# SPDX-License-Identifier: Apache-2.0

"""Client for a remote-helper session control channel."""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from typing import BinaryIO, cast

from zephyr_remote_openocd.controller.authority import (
    SessionAuthority,
    SessionError,
    SessionSnapshot,
)
from zephyr_remote_openocd.controller.cancellation import admit_effect
from zephyr_remote_openocd.controller.deployment import DeploymentResult
from zephyr_remote_openocd.controller.plan import SessionAllocation
from zephyr_remote_openocd.controller.ssh import ManagedSshProcess, SshCommand, _stop_process
from zephyr_remote_openocd.controller.waiting import Notifier, WaitSources, wait_for
from zephyr_remote_openocd.protocol.commands import encode_start
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import EventOrder, decode_terminal, read_message

# The helper gives a supervised child five seconds to exit after SIGTERM,
# followed by up to four seconds joining its relay threads and workspace
# cleanup. Keep the control transport alive through that fallback and remote
# process scheduling/transport overhead.
HELPER_STOP_TIMEOUT = 15.0
HELPER_START_TIMEOUT = 10.0


class HelperConnection:
    """Own one helper control connection and its controller-lifetime lease.

    One reader thread owns the helper's event stream from control acquisition
    until shutdown and reports every event to the shared authority. The
    coordinator never reads that stream; it waits on authority notifications.
    """

    def __init__(
        self,
        ssh_command: SshCommand,
        host: str,
        deployment: DeploymentResult,
        output_handler: Callable[[str, str, bool], None] | None = None,
        *,
        process_start_handler: Callable[[tuple[str, ...]], None] | None = None,
        authority: SessionAuthority,
        notifier: Notifier,
    ) -> None:
        self._ssh_command = ssh_command
        self._host = host
        self._deployment = deployment
        self._output_handler = output_handler
        self._process_start_handler = process_start_handler
        self._authority = authority
        self._notifier = notifier
        self._reader_thread: threading.Thread | None = None
        self._process: ManagedSshProcess | None = None
        self._allocation: SessionAllocation | None = None
        self._order: EventOrder | None = None
        self._closed = False

    @property
    def allocation(self) -> SessionAllocation:
        assert self._allocation is not None
        return self._allocation

    def start_process(
        self,
        process_spec: ProcessSpec,
        listeners: Iterable[ListenerSpec],
        *,
        preferred_address: str | None = None,
    ) -> str | None:
        try:
            return self._start_process(process_spec, listeners, preferred_address=preferred_address)
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def _start_process(
        self,
        process_spec: ProcessSpec,
        listeners: Iterable[ListenerSpec],
        *,
        preferred_address: str | None = None,
    ) -> str | None:
        self._authority.set_policy(process_spec.completion_policy)
        assert self._order is not None
        self._order.policy = process_spec.completion_policy
        listener_specs = tuple(listeners)
        helper = self._process_or_error()
        if helper.stdin is None:
            raise SessionError("helper stdin was not captured")
        frame = encode_start(process_spec, listener_specs, preferred_address=preferred_address)
        # START authorizes remote child creation; recorded helper facts and the
        # operation's cancellation are accounted at one admission point.
        self._authority.admit_process_start(admit_effect)
        self._write_control(helper.stdin.fileno(), frame)
        if process_spec.completion_policy != CompletionPolicy.LIVE_SERVER:
            return None
        return self._await(self._ready_address, "remote process ended before READY")

    def _write_control(self, descriptor: int, data: bytes) -> None:
        """Write without blocking the coordinator on helper-pipe backpressure."""
        os.set_blocking(descriptor, False)
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            try:
                offset += os.write(descriptor, view[offset:])
            except BlockingIOError:
                wait_for(writable=(descriptor,))

    @staticmethod
    def _created(snapshot: SessionSnapshot) -> SessionAllocation | None:
        return snapshot.allocation

    @staticmethod
    def _ready_address(snapshot: SessionSnapshot) -> str | None:
        return snapshot.ready_address

    def _await[T](
        self,
        observe: Callable[[SessionSnapshot], T | None],
        ended: str,
        deadline: float | None = None,
    ) -> T:
        """Wait for a reader-reported fact; recorded failures take precedence.

        The common wait arbitrates the deadline: it dispatches a latched
        cancellation before reporting expiry, and facts recorded before that
        wait returned are re-read before expiry is reported as a timeout.
        """
        expired = False
        while True:
            snapshot = self._authority.snapshot()
            if snapshot.failure is not None:
                raise self._authority.publish_failure() or snapshot.failure
            observed = observe(snapshot)
            if observed is not None:
                return observed
            if snapshot.terminal is not None:
                raise self._authority.publish_failure() or SessionError(ended)
            if expired:
                diagnostic = self._process_or_error().stderr_tail()
                suffix = ": " + diagnostic.decode("utf-8", "replace").strip() if diagnostic else ""
                raise SessionError("remote helper startup timed out" + suffix) from TimeoutError(
                    "remote helper startup deadline expired"
                )
            expired = wait_for(
                sources=WaitSources(notifiers=(self._notifier,)), deadline=deadline
            ).expired

    def observe_status(self) -> None:
        """Record transport status without selecting or publishing a result."""
        if self._process is not None:
            try:
                self._authority.record_control_status(self._process.poll())
            except BaseException as error:
                self._authority.record_failure("CONTROL_OBSERVATION_FAILURE", error)

    def live_processes(self) -> tuple[ManagedSshProcess, ...]:
        """Owned control transport whose exit has not yet been observed."""
        process = self._process
        return () if process is None or process.returncode is not None else (process,)

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        """Build the public wait timeout using this control process's command."""
        return subprocess.TimeoutExpired(self._process_or_error().args, timeout)

    def close(self) -> None:
        """Settle physical resources; all failures belong to the shared authority."""
        if self._closed or self._process is None:
            return
        helper = self._process_or_error()
        self._request_shutdown(helper)
        self._cleanup_control_process(helper)
        self._emit_diagnostic()
        self._resolve_shutdown_result()
        self._closed = True

    def _request_shutdown(self, helper: ManagedSshProcess) -> None:
        """Half-close stdin while retaining reverse output for bounded shutdown."""
        self._authority.cancel()
        try:
            if self._reader_thread is None:
                self._start_event_drain()
        except BaseException as error:
            self._authority.record_failure("READER_START_FAILURE", error)
        try:
            if helper.stdin is not None:
                helper.stdin.close()
        except BaseException as error:
            self._authority.record_failure("CONTROL_CLOSE_FAILURE", error)
        self.observe_status()
        try:
            self._authority.record_control_status(helper.wait(timeout=HELPER_STOP_TIMEOUT))
        except BaseException as error:
            self._authority.record_failure("HELPER_WAIT_FAILURE", error)

    def _cleanup_control_process(self, helper: ManagedSshProcess) -> None:
        """Stop the reader and helper process without abandoning cleanup."""
        try:
            reader_stopped = self._join_reader()
        except BaseException as error:
            self._authority.record_failure("HELPER_CLEANUP_FAILURE", error)
            reader_stopped = False
        try:
            _stop_process(helper, close_streams=reader_stopped)
        except BaseException as error:
            self._authority.record_failure("HELPER_CLEANUP_FAILURE", error)

        if self._reader_thread is not None:
            initially_stopped = reader_stopped
            try:
                reader_stopped = self._join_reader()
            except BaseException as error:
                self._authority.record_failure("HELPER_CLEANUP_FAILURE", error)
                reader_stopped = False
            if reader_stopped and not initially_stopped:
                try:
                    _stop_process(helper, close_streams=True)
                except BaseException as error:
                    self._authority.record_failure("HELPER_CLEANUP_FAILURE", error)
            if not reader_stopped:
                reader_error = SessionError("helper event reader did not stop")
                self._authority.record_failure("HELPER_CLEANUP_FAILURE", reader_error)

    def _resolve_shutdown_result(self) -> None:
        """Record missing terminal information independently of transport status."""
        if self._authority.snapshot().terminal is None:
            self._authority.record_failure(
                "MISSING_TERMINAL",
                SessionError("helper shutdown did not produce SESSION_ENDED"),
                key="missing_terminal",
            )
        self.observe_status()

    def acquire(self) -> None:
        """Acquire control resources; the owning caller must close on failure."""
        try:
            self._acquire()
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def _acquire(self) -> None:
        command = f"python3 {shlex.quote(self._deployment.path)} control"
        self._order = EventOrder()
        # Transport registration runs inside popen's rollback guard.
        self._ssh_command.popen(self._host, command, on_acquired=self._adopt_control)
        if self._process_or_error().stdout is None:
            raise SessionError("helper stdout was not captured")
        self._start_event_drain()
        self._allocation = self._await(
            self._created,
            "helper ended before session creation",
            time.monotonic() + HELPER_START_TIMEOUT,
        )

    def _adopt_control(self, process: ManagedSshProcess) -> None:
        self._process = process

    def _read_event(self) -> dict:
        helper = self._process_or_error()
        try:
            message = read_message(cast(BinaryIO, helper.stdout))
        except EOFError as error:
            diagnostic = helper.stderr_tail()
            raise SessionError(
                "remote helper terminated: " + diagnostic.decode("utf-8", "replace")
            ) from error
        assert self._order is not None
        self._order.accept(message)
        return message

    def _start_event_drain(self) -> None:
        if self._reader_thread is not None:
            return
        reader_thread = threading.Thread(
            target=self._drain_events, name="zro-helper-events", daemon=True
        )
        # Registration precedes start, so no started reader is ever unowned.
        self._reader_thread = reader_thread
        reader_thread.start()

    def _dispatch(self, event: dict) -> None:
        if event["type"] == "SESSION_CREATED":
            self._authority.record_created(
                SessionAllocation(event["session_id"], event["remote_workspace"])
            )
        elif event["type"] == "ATTEMPT" and self._process_start_handler is not None:
            self._process_start_handler(tuple(event["argv"]))
        elif event["type"] == "CHILD_OUTPUT" and self._output_handler is not None:
            self._output_handler(event["stream"], event["payload"], event["line_end"])
        elif event["type"] == "READY":
            self._authority.record_ready(event["remote_address"])
        elif event["type"] == "SESSION_ENDED":
            self._authority.record_terminal(decode_terminal(event))

    def _drain_events(self) -> None:
        try:
            while True:
                self._dispatch(self._read_event())
        except BaseException as error:
            snapshot = self._authority.snapshot()
            if isinstance(error.__cause__, EOFError) and snapshot.terminal is not None:
                return
            self._authority.record_reader_failure(error)

    def _join_reader(self, timeout: float = 2.0) -> bool:
        # A registered reader whose start failed never ran and holds nothing.
        if self._reader_thread is None or not self._reader_thread.is_alive():
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
