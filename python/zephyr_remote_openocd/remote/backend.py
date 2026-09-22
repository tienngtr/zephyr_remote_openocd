# SPDX-License-Identifier: Apache-2.0

"""Production SSH/helper implementation of a remote session."""

from __future__ import annotations

import os
import secrets
import selectors
import shlex
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from typing import BinaryIO, cast

from .deploy import DeploymentResult, deploy_helper
from .model import (
    DuplicateServiceError,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
    StagedEntry,
    validated_services,
)
from .protocol import (
    EventOrder,
    ProtocolError,
    decode_message,
    read_message,
    validate_openocd_version_response,
    validate_staged_response,
    write_start,
    write_stop,
)
from .session import SessionClosedError, SessionError
from .ssh import ManagedSshProcess, SshCommand
from .staging import build_archive

# The helper gives a supervised child five seconds to exit after SIGTERM,
# followed by up to four seconds joining its relay threads and workspace
# cleanup. Keep the control transport alive through that fallback and remote
# process scheduling/transport overhead.
HELPER_STOP_TIMEOUT = 15.0
HELPER_START_TIMEOUT = 10.0
FORWARD_START_TIMEOUT = 10.0
FORWARD_HEALTH_INTERVAL = 0.25
_PROCESS_TERM_TIMEOUT = 5.0
_PROCESS_KILL_TIMEOUT = 1.0


def _add_failure_note(
    primary: BaseException,
    prefix: str,
    secondary: BaseException,
) -> None:
    """Retain an exception and its existing diagnostics on another failure."""
    primary.add_note(f"{prefix}: {secondary}")
    for note in getattr(secondary, "__notes__", ()):
        primary.add_note(f"{prefix} detail: {note}")


def _raise_cleanup_errors(errors: list[BaseException]) -> None:
    """Raise the first cleanup error after retaining subsequent diagnostics."""
    if not errors:
        return
    first, *additional = errors
    for error in additional:
        _add_failure_note(first, "additional cleanup failure", error)
    raise first


def query_remote_openocd_version(
    ssh_command: SshCommand,
    host: str,
    executable: str | Iterable[str],
) -> str:
    deployment = deploy_helper(ssh_command, host)
    argv = executable if isinstance(executable, (tuple, list)) else (executable,)
    encoded = " ".join(shlex.quote(item) for item in argv)
    command = f"python3 {shlex.quote(deployment.path)} openocd-version -- {encoded}"
    result = ssh_command.run(host, command, timeout=30)
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise SessionError(f"remote OpenOCD version query failed ({result.returncode}): " + detail)
    try:
        message = decode_message(result.stdout)
        validate_openocd_version_response(message)
        return message["output"]
    except (KeyError, ProtocolError, ValueError) as error:
        raise SessionError(f"invalid remote OpenOCD version response: {result.stdout!r}") from error


class RemoteSession:
    def __init__(
        self,
        request: RemoteSessionRequest,
        deployment: DeploymentResult,
        output_handler: Callable[[str, str, bool], None] | None = None,
    ):
        self.request = request
        self.deployment = deployment
        self.forwards: list[ManagedSshProcess] = []
        self.closed = False
        self.output_handler = output_handler
        self._openocd_returncode: int | None = None
        self.reader_error: BaseException | None = None
        self.reader_thread: threading.Thread | None = None
        self.descriptor: SessionDescriptor | None = None
        self._terminal_reason: str | None = None
        self._stop_requested = False
        self._state_lock = threading.RLock()
        self._state_changed = threading.Condition(self._state_lock)
        self._services = list(request.services)

    @property
    def openocd_returncode(self) -> int | None:
        return self._openocd_returncode

    @classmethod
    def open(
        cls,
        request: RemoteSessionRequest,
        *,
        output_handler: Callable[[str, str, bool], None] | None = None,
    ) -> RemoteSession:
        deployment = deploy_helper(request.ssh_command, request.host)
        session = cls(request, deployment, output_handler)
        session._open_helper()
        try:
            session._stage(request.staged_files)
            session.descriptor = session._start_process(request.services)
        except BaseException as error:
            try:
                session.close()
            except BaseException as cleanup_error:
                _add_failure_note(error, "startup failure cleanup also failed", cleanup_error)
            raise
        return session

    def _open_helper(self) -> None:
        command = f"python3 {shlex.quote(self.deployment.path)} control"
        self.helper_process = self.request.ssh_command.popen(self.request.host, command)
        try:
            if self.helper_process.stdout is None:
                raise SessionError("helper stdout was not captured")
            self._order = EventOrder()
            created = self._read_event(time.monotonic() + HELPER_START_TIMEOUT)
            if created["type"] != "SESSION_CREATED":
                raise ProtocolError("helper did not begin with SESSION_CREATED")
            self.allocation = SessionAllocation(created["session_id"], created["remote_workspace"])
        except BaseException as error:
            try:
                self._stop_process(self.helper_process)
            except BaseException as cleanup_error:
                _add_failure_note(error, "helper startup cleanup also failed", cleanup_error)
            raise

    def _read_event(self, deadline: float | None = None) -> dict:
        stream = cast(BinaryIO, self.helper_process.stdout)
        try:
            message = (
                read_message(stream)
                if deadline is None
                else self._read_initial_message(stream, deadline)
            )
        except EOFError as error:
            diagnostic = self.helper_process.stderr_tail()
            raise SessionError(
                "remote helper terminated: " + diagnostic.decode("utf-8", "replace")
            ) from error
        except TimeoutError as error:
            diagnostic = self.helper_process.stderr_tail()
            suffix = ": " + diagnostic.decode("utf-8", "replace").strip() if diagnostic else ""
            raise SessionError("remote helper startup timed out" + suffix) from error
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

    def _stage(self, files: Iterable[StagedEntry]):
        archive = build_archive(files)
        try:
            command = (
                f"python3 {shlex.quote(self.deployment.path)} stage "
                f"{shlex.quote(self.allocation.remote_workspace)}"
            )
            result = self.request.ssh_command.run_stream(
                self.request.host, command, archive.stream, timeout=60
            )
        finally:
            archive.stream.close()
        if result.returncode:
            raise SessionError(
                f"remote staging failed ({result.returncode}): "
                + result.stderr.decode("utf-8", "replace").strip()
            )
        try:
            message = decode_message(result.stdout)
            validate_staged_response(message)
            if tuple(message.get("files", ())) != archive.files:
                raise ValueError("remote staged-file confirmation differs from manifest")
            if tuple(message.get("directories", ())) != archive.directories:
                raise ValueError("remote staged-directory confirmation differs from manifest")
            if message["byte_count"] != archive.byte_count:
                raise ValueError("remote staged-file byte count differs from manifest")
            if message["sha256"] != archive.sha256:
                raise ValueError("remote staged-file digest differs from manifest")
            return message
        except (ProtocolError, ValueError) as error:
            raise SessionError(f"invalid remote staging response: {result.stdout!r}") from error

    @staticmethod
    def _preflight(service: Service) -> str | None:
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", service.local_port))
            return None
        except OSError as error:
            return (
                f"local port 127.0.0.1:{service.local_port} for {service.name} "
                f"appears unavailable: {error}"
            )

    @staticmethod
    def _forward_ready_command(token: str) -> str:
        code = f"import sys; print({token!r}, flush=True); sys.stdin.buffer.read()"
        return "python3 -c " + shlex.quote(code)

    @staticmethod
    def _await_forward_ready(process: ManagedSshProcess, token: str, deadline: float) -> bool:
        """Wait for the readiness sentinel from this exact SSH process."""
        if process.stdout is None:
            return False
        token_bytes = token.encode()
        pending = b""
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None and time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                if not selector.select(min(0.05, remaining)):
                    continue
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    return False
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.rstrip(b"\r") == token_bytes:
                        return True
            return False
        finally:
            selector.close()

    @staticmethod
    def _forward_diagnostic(process: ManagedSshProcess) -> str:
        try:
            return process.stderr_tail().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return ""

    def _start_process(self, services: Iterable[Service]) -> SessionDescriptor:
        service_list = tuple(services)
        process = self.request.process
        if self.helper_process.stdin is None:
            raise SessionError("helper stdin was not captured")
        write_start(
            cast(BinaryIO, self.helper_process.stdin),
            process,
            service_list,
        )
        address = self._await_process_ready()
        self._start_event_drain()
        if service_list:
            self._start_forwards(service_list, address)
        self.descriptor = SessionDescriptor(self.allocation, address)
        return self.descriptor

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
        if self.reader_thread is not None:
            return
        self.reader_thread = threading.Thread(target=self._drain_events, daemon=True)
        self.reader_thread.start()

    def forward(self, services: Iterable[Service]) -> None:
        if self.closed:
            raise SessionClosedError("remote session is closed")
        if self.descriptor is None:
            raise SessionError("remote session is not ready for additional forwarding")
        service_list = tuple(services)
        if not service_list:
            return
        try:
            validated_services((*self._services, *service_list))
        except DuplicateServiceError as error:
            raise SessionError(f"{error.subject} must remain unique") from error
        self._start_forwards(service_list, self.descriptor.remote_address)
        self._services.extend(service_list)

    def check_openocd_exit(self) -> int | None:
        if self.closed:
            return self._openocd_returncode
        result = self._recorded_openocd_exit()
        if result is not None:
            return result
        self._check_forward_health()
        return self._recorded_openocd_exit()

    def _recorded_openocd_exit(self) -> int | None:
        with self._state_changed:
            if self.reader_error is not None:
                raise SessionError(
                    f"helper event stream failed: {self.reader_error}"
                ) from self.reader_error
            return self._openocd_returncode

    def _check_forward_health(self) -> None:
        for process in self.forwards:
            forward_status = process.poll()
            if forward_status is not None:
                detail = self._forward_diagnostic(process)
                suffix = f": {detail}" if detail else ""
                raise SessionError(f"SSH forwarding exited with status {forward_status}{suffix}")

    def _start_forwards(self, service_list, address):
        advisories = [message for service in service_list if (message := self._preflight(service))]
        for service in service_list:
            spec = f"127.0.0.1:{service.local_port}:{address}:{service.remote_port}"
            token = "ZRO_FORWARD_" + secrets.token_hex(16)
            process = self.request.ssh_command.popen(
                self.request.host,
                self._forward_ready_command(token),
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                spec,
            )
            self.forwards.append(process)
            deadline = time.monotonic() + FORWARD_START_TIMEOUT
            connected = self._await_forward_ready(process, token, deadline)
            if process.poll() is not None:
                detail = self._forward_diagnostic(process)
                prefix = "; ".join(advisories)
                raise SessionError(
                    (prefix + "; " if prefix else "")
                    + f"SSH forwarding failed for {service.name} on "
                    f"127.0.0.1:{service.local_port} ({process.returncode}): "
                    f"{detail}"
                )
            if not connected:
                error = SessionError(
                    f"SSH forwarding did not become ready for {service.name} on "
                    f"127.0.0.1:{service.local_port}"
                )
                detail = self._forward_diagnostic(process)
                suffix = f": {detail}" if detail else ""
                error.args = (error.args[0] + suffix,)
                raise error

    def wait_for_openocd_exit(self, timeout: float | None = None) -> int:
        if self.closed:
            if self._openocd_returncode is None:
                raise SessionClosedError("remote session closed without a natural OpenOCD exit")
            return self._openocd_returncode
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self.check_openocd_exit()
            if result is not None:
                return result
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(self.helper_process.args, timeout or 0.0)
            health_wait = FORWARD_HEALTH_INTERVAL if self.forwards else remaining
            wait_timeout = (
                health_wait if remaining is None else min(remaining, health_wait or remaining)
            )
            with self._state_changed:
                if self.reader_error is not None or self._openocd_returncode is not None:
                    continue
                self._state_changed.wait(wait_timeout)

    def _dispatch(self, event: dict) -> None:
        if event["type"] == "CHILD_OUTPUT" and self.output_handler is not None:
            self.output_handler(
                event["stream"],
                event["payload"],
                event["line_end"],
            )
        elif event["type"] == "SESSION_CLOSED":
            with self._state_changed:
                self._terminal_reason = event["reason"]
                if event["reason"] == "process_exit":
                    self._openocd_returncode = int(event["returncode"])
                elif not self._stop_requested:
                    self.reader_error = SessionError(
                        "helper reported SESSION_CLOSED(reason='requested') before STOP"
                    )
                self._state_changed.notify_all()

    def _drain_events(self) -> None:
        try:
            while True:
                event = self._read_event()
                self._dispatch(event)
        except BaseException as error:
            with self._state_changed:
                if isinstance(error.__cause__, EOFError):
                    helper_status = self.helper_process.poll()
                    if self._terminal_reason is not None and helper_status in (0, None):
                        return
                    if self._terminal_reason is None:
                        detail = (
                            "remote helper exited without a terminal event"
                            if helper_status is None
                            else f"remote helper exited with status {helper_status} "
                            "without a terminal event"
                        )
                    else:
                        detail = (
                            f"remote helper exited with status {helper_status} "
                            f"after {self._terminal_reason} shutdown"
                        )
                    failure = SessionError(detail)
                    failure.__cause__ = error.__cause__
                    error = failure
                self.reader_error = error
                self._state_changed.notify_all()

    @staticmethod
    def _stop_process(process: ManagedSshProcess, *, close_streams: bool = True) -> None:
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        graceful_timeout: BaseException | None = None
        process_dead = False

        def record_process_error(error: BaseException) -> None:
            nonlocal primary_error
            if primary_error is None:
                primary_error = error
            else:
                cleanup_errors.append(error)

        try:
            process_dead = process.poll() is not None
        except BaseException as error:
            record_process_error(error)

        if not process_dead:
            termination_failed = False
            try:
                process.terminate()
            except BaseException as error:
                termination_failed = True
                record_process_error(error)
            if termination_failed:
                try:
                    process_dead = process.poll() is not None
                except BaseException as error:
                    record_process_error(error)
            else:
                try:
                    process.wait(timeout=_PROCESS_TERM_TIMEOUT)
                    process_dead = True
                except subprocess.TimeoutExpired as error:
                    # A graceful timeout is the expected trigger for the kill
                    # fallback.  It becomes diagnostic only if that fallback
                    # also fails.
                    graceful_timeout = error
                except BaseException as error:
                    record_process_error(error)

            if not process_dead:
                kill_failed = False
                try:
                    process.kill()
                except BaseException as error:
                    kill_failed = True
                    record_process_error(error)
                if not kill_failed:
                    try:
                        process.wait(timeout=_PROCESS_KILL_TIMEOUT)
                        process_dead = True
                    except BaseException as error:
                        record_process_error(error)
                if not process_dead:
                    try:
                        process_dead = process.poll() is not None
                    except BaseException as error:
                        record_process_error(error)

        if not process_dead and primary_error is None:
            record_process_error(RuntimeError("process did not exit during cleanup"))
        if graceful_timeout is not None and primary_error is not None:
            cleanup_errors.append(graceful_timeout)

        try:
            process.close_stderr()
        except BaseException as error:
            cleanup_errors.append(error)
        if close_streams:
            streams = [process.stdin, process.stdout]
            for stream in streams:
                if stream is None or stream.closed:
                    continue
                try:
                    stream.close()
                except BaseException as error:
                    cleanup_errors.append(error)
        if primary_error is not None:
            for cleanup_failure in cleanup_errors:
                _add_failure_note(primary_error, "process cleanup also failed", cleanup_failure)
            raise primary_error
        _raise_cleanup_errors(cleanup_errors)

    def _close_forwards(self) -> None:
        pending = self.forwards
        self.forwards = []
        errors = []
        for process in pending:
            try:
                self._stop_process(process)
            except BaseException as error:
                errors.append(error)
        _raise_cleanup_errors(errors)

    def _join_reader(self, timeout: float = 2.0) -> bool:
        reader = self.reader_thread
        if reader is None:
            return True
        reader.join(timeout=timeout)
        return not reader.is_alive()

    def _helper_reader_failure(self) -> BaseException | None:
        with self._state_changed:
            reader_error = self.reader_error
        if reader_error is None:
            return None
        failure = SessionError(f"helper event stream failed: {reader_error}")
        failure.__cause__ = reader_error
        return failure

    def _emit_helper_diagnostic(self) -> None:
        """Surface the bounded helper stderr tail without changing close status."""
        if self.output_handler is None:
            return
        try:
            diagnostic = self.helper_process.stderr_tail()
            if not diagnostic:
                return
            payload = diagnostic.decode("utf-8", "replace")
            fragments = payload.split("\n")
            for index, fragment in enumerate(fragments):
                line_end = index < len(fragments) - 1
                if fragment or line_end:
                    self.output_handler("stderr", fragment, line_end)
        except BaseException:
            # Diagnostics are best effort and must not mask a successful close.
            return

    def _close_helper(self) -> tuple[BaseException | None, list[BaseException]]:
        """Stop the helper and return logical and mechanical cleanup failures.

        A helper terminal event is part of the STOP contract.  Mechanical
        process cleanup is kept separate so that an event/protocol failure is
        never replaced by a later terminate, wait, or stream-close failure.
        """

        helper = self.helper_process
        logical_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []

        def terminal_reason() -> str | None:
            with self._state_lock:
                return self._terminal_reason

        terminal_before_stop = terminal_reason()
        helper_status = helper.poll()

        if terminal_before_stop == "requested":
            logical_error = SessionError(
                "helper reported SESSION_CLOSED(reason='requested') before STOP"
            )
        # A process-exit terminal event means that the helper has already
        # handled the remote process.  Do not send a second STOP merely
        # because a fake or SSH wrapper has not reaped its own process yet.
        elif helper_status is None and terminal_before_stop is None:
            if self.reader_thread is None:
                self._start_event_drain()
            if helper.stdin is None:
                logical_error = SessionError("helper stdin was not captured")
            else:

                def request_stop() -> None:
                    nonlocal logical_error
                    nonlocal terminal_before_stop
                    terminal_before_stop = terminal_reason()
                    if terminal_before_stop == "requested":
                        logical_error = SessionError(
                            "helper reported SESSION_CLOSED(reason='requested') before STOP"
                        )
                        return
                    if terminal_before_stop == "process_exit":
                        return
                    try:
                        write_stop(cast(BinaryIO, helper.stdin))
                    except BaseException as error:
                        logical_error = error
                    else:
                        self._stop_requested = True

                with self._state_lock:
                    request_stop()
                try:
                    helper.stdin.close()
                except BaseException as error:
                    cleanup_errors.append(error)
                if logical_error is None:
                    try:
                        helper.wait(timeout=HELPER_STOP_TIMEOUT)
                    except subprocess.TimeoutExpired as error:
                        logical_error = error
                    except BaseException as error:
                        logical_error = error

        # Let the reader consume the requested terminal event before the
        # process streams are forcibly closed.  A bounded second join below
        # handles a helper that does not close its output after termination.
        reader_stopped = self._join_reader()

        try:
            self._stop_process(helper, close_streams=reader_stopped)
        except BaseException as error:
            cleanup_errors.append(error)

        if self.reader_thread is not None:
            initially_stopped = reader_stopped
            reader_stopped = self._join_reader()
            if reader_stopped and not initially_stopped:
                try:
                    self._stop_process(helper, close_streams=True)
                except BaseException as error:
                    cleanup_errors.append(error)
            if not reader_stopped:
                reader_cleanup_error = SessionError("helper event reader did not stop")
                cleanup_errors.append(reader_cleanup_error)

        self._emit_helper_diagnostic()

        with self._state_changed:
            reader_eof = isinstance(
                self.reader_error.__cause__ if self.reader_error is not None else None,
                EOFError,
            )
        reader_failure = self._helper_reader_failure()
        if reader_failure is not None and not reader_eof:
            logical_error = logical_error or reader_failure

        terminal = terminal_reason()
        if logical_error is None:
            helper_status = helper.poll()
            if terminal not in {"requested", "process_exit"}:
                logical_error = SessionError(
                    "helper shutdown did not produce "
                    "SESSION_CLOSED(reason='requested' or 'process_exit') "
                    f"(terminal={terminal!r}, "
                    f"exit={helper_status!r})"
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

        return logical_error, cleanup_errors

    def close(self) -> None:
        if self.closed:
            return

        forward_errors: list[BaseException] = []
        try:
            self._close_forwards()
        except BaseException as error:
            forward_errors.append(error)

        helper_error: BaseException | None = None
        helper_cleanup_errors: list[BaseException] = []
        try:
            helper_error, helper_cleanup_errors = self._close_helper()
        except BaseException as error:
            helper_error = error

        errors = forward_errors
        if helper_error is not None:
            errors.append(helper_error)
        elif helper_cleanup_errors:
            errors.extend(helper_cleanup_errors)
        self.closed = True
        if errors:
            _raise_cleanup_errors(errors)
