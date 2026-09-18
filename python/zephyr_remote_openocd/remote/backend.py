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
from .staging import build_archive


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
        output_handler: Callable[[str, str], None] | None = None,
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
        output_handler=None,
    ):
        self.request = request
        self.deployment = deployment
        self.forward_start_timeout = forward_start_timeout
        self.forwards: list[subprocess.Popen[bytes]] = []
        self.closed = False
        self.output_handler = output_handler
        self.process_returncode: int | None = None
        self.reader_error: BaseException | None = None
        self.reader_thread: threading.Thread | None = None
        self.events: list[dict[str, object]] = []
        self.descriptor: SessionDescriptor | None = None
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
            diagnostic = b""
            if self.helper_process.stderr is not None:
                diagnostic = self.helper_process.stderr.read()
            raise SessionError(
                "remote helper terminated: " + diagnostic.decode("utf-8", "replace")
            ) from error
        self._order.accept(message)
        self.events.append(message)
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
    def _preflight(port: int) -> str | None:
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port))
            return None
        except OSError as error:
            return f"local port 127.0.0.1:{port} appears unavailable: {error}"

    @staticmethod
    def _forward_ready_command(token: str) -> str:
        code = f"import sys; print({token!r}, flush=True); sys.stdin.buffer.read()"
        return "python3 -c " + shlex.quote(code)

    @staticmethod
    def _await_forward_ready(process: subprocess.Popen[bytes], token: str, deadline: float) -> bool:
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
    def _forward_diagnostic(process: subprocess.Popen[bytes]) -> str:
        if process.stderr is None:
            return ""
        try:
            return process.stderr.read().decode("utf-8", "replace").strip()
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
                        message
                        for item in service_list
                        if (message := self._preflight(item.local_port))
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
                    self.forwards.append(process)
                    error.add_note(f"forward rollback also failed: {cleanup_error}")
            raise

    def poll(self) -> int | None:
        if self.process_returncode is not None:
            return self.process_returncode
        if self.reader_error is not None:
            raise SessionError(
                f"helper event stream failed: {self.reader_error}"
            ) from self.reader_error
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
            message for item in service_list if (message := self._preflight(item.local_port))
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
                    + f"SSH forwarding failed ({process.returncode}): "
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
            if self.process_returncode is not None:
                return self.process_returncode
            return result
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"session cleanup also failed: {cleanup_error}")
            raise

    def _dispatch(self, event: dict) -> None:
        if event["type"] == "CHILD_OUTPUT" and self.output_handler is not None:
            self.output_handler(event["stream"], event.get("payload", ""))
        elif event["type"] == "SESSION_CLOSED" and event["reason"] == "process_exit":
            self.process_returncode = int(event["returncode"])

    def _drain_events(self) -> None:
        try:
            while True:
                event = self._read_event()
                self._dispatch(event)
                if event["type"] == "SESSION_CLOSED":
                    return
        except EOFError:
            return
        except BaseException as error:
            self.reader_error = error

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes], *, close_streams: bool = True) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if close_streams:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def _close_forwards(self) -> None:
        pending = self.forwards
        self.forwards = []
        errors = []
        for process in pending:
            try:
                self._stop_process(process)
            except BaseException as error:
                self.forwards.append(process)
                errors.append(error)
        _raise_cleanup_errors(errors)

    def close(self) -> None:
        if self.closed:
            return
        errors = []
        try:
            self._close_forwards()
        except BaseException as error:
            errors.append(error)
        try:
            if self.helper_process.poll() is None and self.helper_process.stdin is not None:
                try:
                    write_stop(cast(BinaryIO, self.helper_process.stdin))
                    self.helper_process.stdin.close()
                    self.helper_process.wait(timeout=5)
                except (BrokenPipeError, OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                except BaseException as error:
                    errors.append(error)
        except BaseException as error:
            errors.append(error)
        try:
            self._stop_process(self.helper_process)
        except BaseException as error:
            errors.append(error)
        if errors:
            _raise_cleanup_errors(errors)
        self.closed = True
