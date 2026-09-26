# SPDX-License-Identifier: Apache-2.0

"""Local SSH-forward lifecycle management."""

from __future__ import annotations

import os
import secrets
import selectors
import shlex
import socket
import time
from collections.abc import Iterable

from .cleanup import _add_failure_note, _raise_cleanup_errors
from .model import DuplicateServiceError, Service, validated_services
from .session import SessionError
from .ssh import ManagedSshProcess, SshCommand, _stop_process

FORWARD_START_TIMEOUT = 10.0
FORWARD_HEALTH_INTERVAL = 0.25


class _ForwardManager:
    """Own local SSH forwards for one remote session."""

    def __init__(self, ssh_command: SshCommand, host: str):
        self._ssh_command = ssh_command
        self._host = host
        self._processes: list[ManagedSshProcess] = []
        self._services: list[Service] = []

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
        """Wait for the readiness sentinel from this exact SSH process."""
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
        try:
            advisories = [
                message for service in service_list if (message := self._preflight(service))
            ]
            for service in service_list:
                spec = f"127.0.0.1:{service.local_port}:{remote_address}:{service.remote_port}"
                sentinel = "ZRO_FORWARD_" + secrets.token_hex(16)
                process = self._ssh_command.popen(
                    self._host,
                    self._ready_command(sentinel),
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-L",
                    spec,
                )
                pending_processes.append(process)
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
        except BaseException as error:
            for process in pending_processes:
                try:
                    _stop_process(process)
                except BaseException as cleanup_error:
                    _add_failure_note(
                        error,
                        "forward startup cleanup also failed",
                        cleanup_error,
                    )
            raise
        self._processes = committed_processes
        self._services = committed_services

    def check_health(self) -> None:
        """Raise if an owned SSH forwarding process has exited."""
        for process in self._processes:
            forward_status = process.poll()
            if forward_status is not None:
                detail = self._diagnostic(process)
                suffix = f": {detail}" if detail else ""
                raise SessionError(f"SSH forwarding exited with status {forward_status}{suffix}")

    def close(self) -> None:
        """Attempt to dispose every owned forward once."""
        pending = self._processes
        self._processes = []
        errors = []
        for process in pending:
            try:
                _stop_process(process)
            except BaseException as error:
                errors.append(error)
        _raise_cleanup_errors(errors)
