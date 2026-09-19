# SPDX-License-Identifier: Apache-2.0

"""Production SSH/helper implementation of the session backend."""

from __future__ import annotations

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
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
    StagedEntry,
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
from .session import BackendSession, SessionBackend, SessionError
from .ssh import ManagedSshProcess
from .staging import build_archive

# The helper gives a supervised child five seconds to exit after SIGTERM,
# followed by up to four seconds joining its relay threads and workspace
# cleanup. Keep the control transport alive through that fallback and remote
# process scheduling/transport overhead.
HELPER_STOP_TIMEOUT = 15.0
_PROCESS_TERM_TIMEOUT = 5.0
_PROCESS_KILL_TIMEOUT = 1.0


def _raise_cleanup_errors(errors: list[BaseException]) -> None:
    """Raise the first cleanup error after retaining subsequent diagnostics."""
    if not errors:
        return
    first, *additional = errors
    for error in additional:
        first.add_note(f"additional cleanup failure: {error}")
    raise first


class SshHelperBackend(SessionBackend):
    def __init__(
        self,
        *,
        forward_start_timeout: float = 10.0,
        output_handler: Callable[[str, str, bool], None] | None = None,
    ):
        self.forward_start_timeout = forward_start_timeout
        self.output_handler = output_handler

    def create(self, request: RemoteSessionRequest) -> BackendSession:
        deployment = deploy_helper(request.ssh_command, request.host)
        return SshHelperSession(
            request, deployment, self.forward_start_timeout, self.output_handler
        )

    def openocd_version(self, ssh_command, host: str, executable) -> str:
        deployment = deploy_helper(ssh_command, host)
        argv = executable if isinstance(executable, (tuple, list)) else (executable,)
        encoded = " ".join(shlex.quote(item) for item in argv)
        command = f"python3 {shlex.quote(deployment.path)} openocd-version -- {encoded}"
        result = ssh_command.run(host, command, timeout=30)
        if result.returncode:
            detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
            raise SessionError(
                f"remote OpenOCD version query failed ({result.returncode}): " + detail
            )
        try:
            message = decode_message(result.stdout)
            validate_openocd_version_response(message)
            return message["output"]
        except (KeyError, ProtocolError, ValueError) as error:
            raise SessionError(
                f"invalid remote OpenOCD version response: {result.stdout!r}"
            ) from error


class SshHelperSession(BackendSession):
    def __init__(
        self,
        request: RemoteSessionRequest,
        deployment: DeploymentResult,
        forward_start_timeout: float,
        output_handler: Callable[[str, str, bool], None] | None = None,
    ):
        self.request = request
        self.deployment = deployment
        self.forward_start_timeout = forward_start_timeout
        self.forwards: list[ManagedSshProcess] = []
        self.closed = False
        self.output_handler = output_handler
        self.process_returncode: int | None = None
        self.reader_error: BaseException | None = None
        self.reader_thread: threading.Thread | None = None
        self.descriptor: SessionDescriptor | None = None
        self._terminal_reason: str | None = None
        self._state_lock = threading.RLock()
        command = f"python3 {shlex.quote(deployment.path)} control"
        self.helper_process = request.ssh_command.popen(request.host, command)
        try:
            if self.helper_process.stdout is None:
                raise SessionError("helper stdout was not captured")
            self._order = EventOrder()
            created = self._read_event()
            if created["type"] != "SESSION_CREATED":
                raise ProtocolError("helper did not begin with SESSION_CREATED")
            self.allocation = SessionAllocation(created["session_id"], created["remote_workspace"])
        except BaseException as error:
            try:
                self._stop_process(self.helper_process)
            except BaseException as cleanup_error:
                error.add_note(f"helper startup cleanup also failed: {cleanup_error}")
            raise

    def _read_event(self) -> dict:
        try:
            message = read_message(cast(BinaryIO, self.helper_process.stdout))
        except EOFError as error:
            diagnostic = self.helper_process.stderr_tail(wait=True)
            raise SessionError(
                "remote helper terminated: " + diagnostic.decode("utf-8", "replace")
            ) from error
        self._order.accept(message)
        if message["type"] == "ERROR":
            raise SessionError(f"remote helper error: {message.get('message', 'unknown error')}")
        return message

    def stage(self, files: Iterable[StagedEntry]):
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
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None and time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                if not selector.select(min(0.05, remaining)):
                    continue
                line = process.stdout.readline()
                if not line:
                    return False
                if line.decode("utf-8", "replace").rstrip("\r\n") == token:
                    return True
            return False
        finally:
            selector.close()

    @staticmethod
    def _forward_diagnostic(process: ManagedSshProcess) -> str:
        try:
            return process.stderr_tail(wait=True).decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return ""

    def start(self, services: Iterable[Service]) -> SessionDescriptor:
        service_list = tuple(services)
        if self.request.process is None and not service_list:
            raise SessionError("at least one service is required for a fake session")
        process = self.request.process or self._fake_process(service_list)
        if self.helper_process.stdin is None:
            raise SessionError("helper stdin was not captured")
        write_start(
            cast(BinaryIO, self.helper_process.stdin),
            process,
            service_list,
        )
        address = self._await_process_ready()
        if service_list:
            try:
                advisories = None
                if self.request.process is None:
                    advisories = [
                        message for service in service_list if (message := self._preflight(service))
                    ]
                self._start_forwards(service_list, address, advisories)
            except BaseException as error:
                try:
                    self._close_forwards()
                except BaseException as cleanup_error:
                    error.add_note(f"forward startup cleanup also failed: {cleanup_error}")
                raise
        self._start_event_drain()
        self.descriptor = SessionDescriptor(self.allocation, address)
        return self.descriptor

    def _fake_process(self, services: tuple[Service, ...]):
        return RemoteProcess(
            (
                "python3",
                self.deployment.path,
                "fake-child",
                "{address}",
                *(str(service.remote_port) for service in services),
            ),
            readiness_marker="ZRO_FAKE_READY",
            literal_prefix=3,
        )

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
        if self.closed or self.descriptor is None:
            raise SessionError("remote session is not ready for additional forwarding")
        service_list = tuple(services)
        if not service_list:
            return
        before = len(self.forwards)
        try:
            self._start_forwards(service_list, self.descriptor.remote_address)
        except BaseException as error:
            added = self.forwards[before:]
            del self.forwards[before:]
            for process in added:
                try:
                    self._stop_process(process)
                except BaseException as cleanup_error:
                    error.add_note(f"forward rollback also failed: {cleanup_error}")
            raise

    def poll(self) -> int | None:
        if self.reader_error is not None:
            raise SessionError(
                f"helper event stream failed: {self.reader_error}"
            ) from self.reader_error
        if self.process_returncode is not None:
            helper_status = self.helper_process.poll()
            if helper_status is None:
                return self.process_returncode
            return helper_status if helper_status else self.process_returncode
        helper_status = self.helper_process.poll()
        if (
            helper_status is not None
            and self.reader_thread is not None
            and self.reader_thread.is_alive()
        ):
            self.reader_thread.join()
            return self.poll()
        if helper_status is not None:
            return helper_status or 1
        if any(process.poll() is not None for process in self.forwards):
            return 1
        return None

    def _start_forwards(self, service_list, address, advisories=None):
        advisories = advisories or [
            message for service in service_list if (message := self._preflight(service))
        ]
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
            deadline = time.monotonic() + self.forward_start_timeout
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
                detail = ""
                try:
                    self._stop_process(process, close_streams=False)
                    detail = self._forward_diagnostic(process)
                except BaseException as cleanup_error:
                    error.add_note(f"forward cleanup also failed: {cleanup_error}")
                try:
                    self._stop_process(process)
                except BaseException as cleanup_error:
                    error.add_note(f"forward cleanup also failed: {cleanup_error}")
                suffix = f": {detail}" if detail else ""
                error.args = (error.args[0] + suffix,)
                raise error

    def wait(self, timeout: float | None = None) -> int:
        try:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                result = self.poll()
                if result is not None:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(self.helper_process.args, timeout or 0.0)
                time.sleep(0.05)
            if self.reader_thread is not None:
                self.reader_thread.join(timeout=2)
            if self.reader_error is not None:
                raise SessionError(
                    f"helper event stream failed: {self.reader_error}"
                ) from self.reader_error
            return result
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"session cleanup also failed: {cleanup_error}")
            raise

    def _dispatch(self, event: dict) -> None:
        if event["type"] == "CHILD_OUTPUT" and self.output_handler is not None:
            self.output_handler(
                event["stream"],
                event["payload"],
                event["line_end"],
            )
        elif event["type"] == "SESSION_CLOSED":
            with self._state_lock:
                self._terminal_reason = event["reason"]
                if event["reason"] == "process_exit":
                    self.process_returncode = int(event["returncode"])

    def _drain_events(self) -> None:
        try:
            while True:
                event = self._read_event()
                self._dispatch(event)
        except EOFError:
            return
        except SessionError as error:
            if isinstance(error.__cause__, EOFError):
                return
            self.reader_error = error
        except BaseException as error:
            self.reader_error = error

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
                primary_error.add_note(f"process cleanup also failed: {cleanup_failure}")
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
        reader_error = self.reader_error
        if reader_error is None:
            return None
        failure = SessionError(f"helper event stream failed: {reader_error}")
        failure.__cause__ = reader_error
        return failure

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
        stop_requested = False
        helper_status = helper.poll()
        reader_thread = self.reader_thread
        can_validate_events = helper.stdout is not None or reader_thread is not None

        if terminal_before_stop == "requested":
            logical_error = SessionError(
                "helper reported SESSION_CLOSED(reason='requested') before STOP"
            )
        # A process-exit terminal event means that the helper has already
        # handled the remote process.  Do not send a second STOP merely
        # because a fake or SSH wrapper has not reaped its own process yet.
        elif helper_status is None and terminal_before_stop is None:
            if reader_thread is None and helper.stdout is not None:
                self._start_event_drain()
                reader_thread = self.reader_thread
            if helper.stdin is None:
                if can_validate_events:
                    logical_error = SessionError("helper stdin was not captured")
            else:

                def request_stop() -> None:
                    nonlocal logical_error
                    nonlocal stop_requested, terminal_before_stop
                    terminal_before_stop = terminal_reason()
                    if terminal_before_stop == "requested":
                        logical_error = SessionError(
                            "helper reported SESSION_CLOSED(reason='requested') before STOP"
                        )
                        return
                    if terminal_before_stop == "process_exit":
                        return
                    stop_requested = True
                    try:
                        write_stop(cast(BinaryIO, helper.stdin))
                    except BaseException as error:
                        logical_error = error

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

        reader_failure = self._helper_reader_failure()
        if reader_failure is not None:
            logical_error = logical_error or reader_failure

        terminal = terminal_reason()
        if stop_requested and logical_error is None and can_validate_events:
            if terminal != "requested":
                status = helper.poll()
                logical_error = SessionError(
                    "helper shutdown did not produce "
                    f"SESSION_CLOSED(reason='requested') (terminal={terminal!r}, "
                    f"exit={status!r})"
                )
            elif helper.poll() not in (0, None):
                logical_error = SessionError(
                    f"remote helper exited with status {helper.poll()} after requested shutdown"
                )

        # If close was called after a natural process-exit terminal event,
        # preserve that already-observed outcome.  It is the result returned
        # by wait()/poll(), rather than a requested STOP transaction.
        if (
            not stop_requested
            and terminal is None
            and logical_error is None
            and can_validate_events
        ):
            logical_error = SessionError("helper exited without a terminal SESSION_CLOSED event")

        if logical_error is not None:
            for cleanup_error in cleanup_errors:
                logical_error.add_note(f"helper cleanup also failed: {cleanup_error}")

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

        errors: list[BaseException] = []
        if helper_error is not None:
            errors.append(helper_error)
        elif helper_cleanup_errors:
            errors.extend(helper_cleanup_errors)
        errors.extend(forward_errors)
        self.closed = True
        if errors:
            _raise_cleanup_errors(errors)
