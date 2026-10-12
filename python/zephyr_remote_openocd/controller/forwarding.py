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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from zephyr_remote_openocd.controller.authority import SessionError
from zephyr_remote_openocd.controller.cleanup import _add_failure_note, _raise_cleanup_errors
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec, validated_forwards
from zephyr_remote_openocd.controller.ssh import (
    ManagedSshProcess,
    SshCommand,
    SshForwardSpec,
    SshProcessStartError,
    _stop_process,
)
from zephyr_remote_openocd.protocol.listeners import DuplicateEndpointError

FORWARD_START_TIMEOUT = 10.0
FORWARD_HEALTH_INTERVAL = 0.25


class ForwardStartError(SessionError):
    """Distinguish unavailable forwarding from failed startup rollback."""

    def __init__(
        self,
        forward: ForwardSpec,
        cause: Exception,
        cleanup_errors: tuple[BaseException, ...],
    ) -> None:
        super().__init__(str(cause))
        self.forward = forward
        self.cause = cause
        self.cleanup_errors = cleanup_errors
        for note in getattr(cause, "__notes__", ()):
            self.add_note(note)


@dataclass(frozen=True)
class ForwardFailure:
    forward: ForwardSpec
    returncode: int
    diagnostic: str

    def as_error(self) -> SessionError:
        suffix = f": {self.diagnostic}" if self.diagnostic else ""
        return SessionError(
            f"SSH forwarding for {self.forward.name} on "
            f"127.0.0.1:{self.forward.local_port} exited with status {self.returncode}{suffix}"
        )


@dataclass(frozen=True)
class ForwardAdvisory:
    forward: ForwardSpec
    phase: Literal["startup", "runtime"]
    failure: SessionError


class ForwardManager:
    """Own local SSH forwards for one remote session."""

    def __init__(self, ssh_command: SshCommand, host: str):
        self._ssh_command = ssh_command
        self._host = host
        self._processes: list[ManagedSshProcess] = []
        self._pending_processes: list[ManagedSshProcess] = []
        self._forwards: list[ForwardSpec] = []
        self._reported: set[ForwardSpec] = set()

    @property
    def forwards(self) -> tuple[ForwardSpec, ...]:
        return tuple(self._forwards)

    @property
    def has_forwards(self) -> bool:
        return bool(self._processes)

    @staticmethod
    def _preflight(forward: ForwardSpec) -> str | None:
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", forward.local_port))
            return None
        except OSError as error:
            return (
                f"local port 127.0.0.1:{forward.local_port} for {forward.name} "
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

    def start(self, forwards: Iterable[ForwardSpec], remote_address: str) -> None:
        """Create and verify local forwards to remote listeners."""
        if self._pending_processes:
            raise SessionError("forward startup rollback is incomplete; close before restarting")
        forward_specs = tuple(forwards)
        try:
            validated_forwards((*self._forwards, *forward_specs))
        except DuplicateEndpointError as error:
            raise SessionError(f"{error.subject} must remain unique") from error

        # Manager ownership survives an interruption before rollback can enter
        # its protected scope. Pending transports are not published as forwards.
        pending_processes = self._pending_processes
        advisories = [message for forward in forward_specs if (message := self._preflight(forward))]
        active_forward = None
        try:
            for forward in forward_specs:
                active_forward = forward
                sentinel = "ZRO_FORWARD_" + secrets.token_hex(16)
                # Adopt the returned transport before delivering pending SIGINT.
                previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
                try:
                    process = self._ssh_command.popen(
                        self._host,
                        self._ready_command(sentinel),
                        local_forward=SshForwardSpec(
                            forward.local_port, remote_address, forward.remote_port
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
                        + f"SSH forwarding failed for {forward.name} on "
                        f"127.0.0.1:{forward.local_port} ({process.returncode}): "
                        f"{detail}"
                    )
                if not connected:
                    readiness_error = SessionError(
                        f"SSH forwarding did not become ready for {forward.name} on "
                        f"127.0.0.1:{forward.local_port}"
                    )
                    detail = self._diagnostic(process)
                    suffix = f": {detail}" if detail else ""
                    readiness_error.args = (readiness_error.args[0] + suffix,)
                    raise readiness_error
            committed_processes = [*self._processes, *pending_processes]
            committed_forwards = [*self._forwards, *forward_specs]
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            try:
                self._processes = committed_processes
                self._forwards = committed_forwards
                # Committed ownership replaces rollback ownership before unmasking.
                pending_processes.clear()
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except BaseException as error:
            # Defer SIGINT through the batch. If entry is interrupted, close()
            # can still reach every transport in the manager-owned pending set.
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            cleanup_errors = (
                list(error.cleanup_errors) if isinstance(error, SshProcessStartError) else []
            )
            try:
                while pending_processes:
                    process = pending_processes[0]
                    try:
                        _stop_process(process)
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                        _add_failure_note(
                            error,
                            "forward startup cleanup also failed",
                            cleanup_error,
                        )
                    # Retire ownership only after the cleanup attempt and its
                    # diagnostics have completed, including a failed attempt.
                    pending_processes.pop(0)
            finally:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    _add_failure_note(
                        error,
                        "forward startup cleanup also failed",
                        cleanup_error,
                    )
            if active_forward is not None and isinstance(
                error, (SessionError, OSError, subprocess.SubprocessError, SshProcessStartError)
            ):
                raise ForwardStartError(active_forward, error, tuple(cleanup_errors)) from error
            raise

    def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
        """Publish each observed exit before polling another owned process."""
        for forward, process in zip(self._forwards, self._processes, strict=True):
            if forward in self._reported:
                continue
            forward_status = process.poll()
            if forward_status is not None:
                report(ForwardFailure(forward, forward_status, self._diagnostic(process)))
                self._reported.add(forward)

    def close(self) -> None:
        """Attempt to clean up every owned forward once."""
        # Commit can temporarily record a process in both ownership sets.
        pending = [
            *self._processes,
            *(process for process in self._pending_processes if process not in self._processes),
        ]
        self._processes = []
        self._pending_processes = []
        self._forwards = []
        self._reported.clear()
        errors: list[BaseException] = []
        for process in pending:
            try:
                _stop_process(process)
            except BaseException as error:
                errors.append(error)
        _raise_cleanup_errors(errors)
