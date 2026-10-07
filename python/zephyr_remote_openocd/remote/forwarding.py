# SPDX-License-Identifier: Apache-2.0

"""Local SSH-forward lifecycle management."""

from __future__ import annotations

import os
import secrets
import selectors
import shlex
import signal
import socket
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .cleanup import _add_failure_note, _raise_cleanup_errors
from .model import DuplicateServiceError, Service, validated_services
from .session import SessionError
from .ssh import ManagedSshProcess, SshCommand, SshLocalForward, SshProcessStartError, _stop_process

FORWARD_START_TIMEOUT = 10.0
FORWARD_HEALTH_INTERVAL = 0.25


class ForwardStartError(SessionError):
    """Distinguish unavailable forwarding from failed startup rollback."""

    def __init__(
        self,
        service: Service,
        cause: Exception,
        cleanup_errors: tuple[BaseException, ...],
    ) -> None:
        super().__init__(str(cause))
        self.service = service
        self.cause = cause
        self.cleanup_errors = cleanup_errors
        for note in getattr(cause, "__notes__", ()):
            self.add_note(note)


@dataclass(frozen=True)
class ForwardFailure:
    service: Service
    returncode: int
    diagnostic: str

    def as_error(self) -> SessionError:
        suffix = f": {self.diagnostic}" if self.diagnostic else ""
        return SessionError(
            f"SSH forwarding for {self.service.name} on "
            f"127.0.0.1:{self.service.local_port} exited with status {self.returncode}{suffix}"
        )


@dataclass(frozen=True)
class ForwardAdvisory:
    service: Service
    phase: Literal["startup", "runtime"]
    failure: SessionError


class _ForwardManager:
    """Own local SSH forwards for one remote session."""

    def __init__(self, ssh_command: SshCommand, host: str):
        self._ssh_command = ssh_command
        self._host = host
        self._processes: list[ManagedSshProcess] = []
        self._services: list[Service] = []
        self._reported: set[Service] = set()

    @property
    def services(self) -> tuple[Service, ...]:
        return tuple(self._services)

    @property
    def has_forwards(self) -> bool:
        return bool(self._processes)

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
    def _ready_command(sentinel: str) -> str:
        code = f"import sys; print({sentinel!r}, flush=True); sys.stdin.buffer.read()"
        return "python3 -c " + shlex.quote(code)

    @staticmethod
    def _await_ready(process: ManagedSshProcess, sentinel: str, deadline: float) -> bool:
        """Wait for the startup output marker from this exact SSH process."""
        if process.stdout is None:
            return False
        sentinel_bytes = sentinel.encode()
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
                    if line.rstrip(b"\r") == sentinel_bytes:
                        return True
            return False
        finally:
            selector.close()

    @staticmethod
    def _diagnostic(process: ManagedSshProcess) -> str:
        try:
            return process.stderr_tail().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            return ""

    def start(self, services: Iterable[Service], remote_address: str) -> None:
        """Create and verify local forwards for remote services."""
        service_list = tuple(services)
        try:
            validated_services((*self._services, *service_list))
        except DuplicateServiceError as error:
            raise SessionError(f"{error.subject} must remain unique") from error

        pending_processes: list[ManagedSshProcess] = []
        advisories = [message for service in service_list if (message := self._preflight(service))]
        active_service = None
        try:
            for service in service_list:
                active_service = service
                sentinel = "ZRO_FORWARD_" + secrets.token_hex(16)
                # Adopt the returned transport before delivering pending SIGINT.
                previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
                try:
                    process = self._ssh_command.popen(
                        self._host,
                        self._ready_command(sentinel),
                        local_forward=SshLocalForward(
                            service.local_port, remote_address, service.remote_port
                        ),
                    )
                    pending_processes.append(process)
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                connected = self._await_ready(
                    process, sentinel, time.monotonic() + FORWARD_START_TIMEOUT
                )
                if process.poll() is not None:
                    detail = self._diagnostic(process)
                    prefix = "; ".join(advisories)
                    raise SessionError(
                        (prefix + "; " if prefix else "")
                        + f"SSH forwarding failed for {service.name} on "
                        f"127.0.0.1:{service.local_port} ({process.returncode}): "
                        f"{detail}"
                    )
                if not connected:
                    readiness_error = SessionError(
                        f"SSH forwarding did not become ready for {service.name} on "
                        f"127.0.0.1:{service.local_port}"
                    )
                    detail = self._diagnostic(process)
                    suffix = f": {detail}" if detail else ""
                    readiness_error.args = (readiness_error.args[0] + suffix,)
                    raise readiness_error
            committed_processes = [*self._processes, *pending_processes]
            committed_services = [*self._services, *service_list]
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            try:
                self._processes = committed_processes
                self._services = committed_services
                # Committed ownership replaces rollback ownership before unmasking.
                pending_processes.clear()
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except BaseException as error:
            cleanup_errors = (
                list(error.cleanup_errors) if isinstance(error, SshProcessStartError) else []
            )
            for process in pending_processes:
                try:
                    _stop_process(process)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    _add_failure_note(
                        error,
                        "forward startup cleanup also failed",
                        cleanup_error,
                    )
            if active_service is not None and isinstance(
                error, (SessionError, OSError, subprocess.SubprocessError, SshProcessStartError)
            ):
                raise ForwardStartError(active_service, error, tuple(cleanup_errors)) from error
            raise

    def check_health(self) -> tuple[ForwardFailure, ...]:
        """Report newly observed exits without classifying forwarding requirements."""
        failures = []
        for service, process in zip(self._services, self._processes, strict=True):
            if service in self._reported:
                continue
            forward_status = process.poll()
            if forward_status is not None:
                failures.append(ForwardFailure(service, forward_status, self._diagnostic(process)))
                self._reported.add(service)
        return tuple(failures)

    def close(self) -> None:
        """Attempt to clean up every owned forward once."""
        pending = self._processes
        self._processes = []
        self._services = []
        self._reported.clear()
        errors = []
        for process in pending:
            try:
                _stop_process(process)
            except BaseException as error:
                errors.append(error)
        _raise_cleanup_errors(errors)
