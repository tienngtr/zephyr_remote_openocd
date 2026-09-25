# SPDX-License-Identifier: Apache-2.0

"""Production SSH/helper implementation of a remote session."""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable, Iterable

from .cleanup import _add_failure_note, _raise_cleanup_errors
from .deploy import DeploymentResult, deploy_helper
from .forwarding import FORWARD_HEALTH_INTERVAL, _ForwardManager
from .helper_client import _HelperClient
from .model import RemoteSessionRequest, Service, SessionDescriptor, StagedEntry
from .protocol import (
    ProtocolError,
    decode_single_frame,
    validate_helper_event,
    validate_openocd_version_response,
    validate_staged_response,
)
from .session import SessionClosedError, SessionError
from .ssh import SshCommand
from .staging import build_archive


def _one_shot_failure_detail(stdout: bytes, stderr: bytes) -> str:
    """Decode a helper ERROR frame, falling back to transport diagnostics."""
    if stdout:
        try:
            message = decode_single_frame(stdout)
            validate_helper_event(message)
            if message["type"] == "ERROR":
                return f"{message['code']}: {message['message']}"
        except (ProtocolError, ValueError):
            pass
    return (stderr or stdout).decode("utf-8", "replace").strip()


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
        detail = _one_shot_failure_detail(result.stdout, result.stderr)
        raise SessionError(f"remote OpenOCD version query failed ({result.returncode}): " + detail)
    try:
        message = decode_single_frame(result.stdout)
        validate_openocd_version_response(message)
        return message["output"]
    except (KeyError, ProtocolError, ValueError) as error:
        raise SessionError(f"invalid remote OpenOCD version response: {result.stdout!r}") from error


class RemoteSession:
    """Coordinate one helper client and its SSH forwarding processes."""

    def __init__(
        self,
        request: RemoteSessionRequest,
        deployment: DeploymentResult,
        output_handler: Callable[[str, str, bool], None] | None = None,
    ) -> None:
        self.request = request
        self.deployment = deployment
        self._forwards = _ForwardManager(request.ssh_command, request.host)
        self._helper: _HelperClient | None = None
        self.closed = False
        self.descriptor: SessionDescriptor | None = None

    @property
    def openocd_returncode(self) -> int | None:
        return None if self._helper is None else self._helper.openocd_returncode

    @classmethod
    def open(
        cls,
        request: RemoteSessionRequest,
        *,
        output_handler: Callable[[str, str, bool], None] | None = None,
    ) -> RemoteSession:
        deployment = deploy_helper(request.ssh_command, request.host)
        session = cls(request, deployment, output_handler)
        session._helper = _HelperClient.open(
            request.ssh_command,
            request.host,
            deployment,
            output_handler=output_handler,
        )
        try:
            session._stage(request.staged_files)
            session._start_process(request.services)
        except BaseException as error:
            try:
                session.close()
            except BaseException as cleanup_error:
                _add_failure_note(error, "startup failure cleanup also failed", cleanup_error)
            raise
        return session

    def _stage(self, files: Iterable[StagedEntry]):
        helper = self._helper_or_error()
        archive = build_archive(files)
        try:
            command = (
                f"python3 {shlex.quote(self.deployment.path)} stage "
                f"{shlex.quote(helper.allocation.remote_workspace)}"
            )
            result = self.request.ssh_command.run_stream(
                self.request.host, command, archive.stream, timeout=60
            )
        finally:
            archive.stream.close()
        if result.returncode:
            raise SessionError(
                f"remote staging failed ({result.returncode}): "
                + _one_shot_failure_detail(result.stdout, result.stderr)
            )
        try:
            message = decode_single_frame(result.stdout)
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

    def _start_process(self, services: Iterable[Service]) -> SessionDescriptor:
        service_list = tuple(services)
        helper = self._helper_or_error()
        address = helper.start_process(self.request.process, service_list)
        if service_list:
            self._forwards.start(service_list, address)
        self.descriptor = SessionDescriptor(helper.allocation, address)
        return self.descriptor

    def forward(self, services: Iterable[Service]) -> None:
        if self.closed:
            raise SessionClosedError("remote session is closed")
        if self.descriptor is None:
            raise SessionError("remote session is not ready for additional forwarding")
        service_list = tuple(services)
        if service_list:
            self._forwards.start(service_list, self.descriptor.remote_address)

    def check_openocd_exit(self) -> int | None:
        if self.closed:
            return self.openocd_returncode
        helper = self._helper_or_error()
        result = helper.recorded_openocd_exit()
        if result is not None:
            return result
        self._forwards.check_health()
        return helper.recorded_openocd_exit()

    def wait_for_openocd_exit(self, timeout: float | None = None) -> int:
        if self.closed:
            result = self.openocd_returncode
            if result is None:
                raise SessionClosedError("remote session closed without a natural OpenOCD exit")
            return result
        helper = self._helper_or_error()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self.check_openocd_exit()
            if result is not None:
                return result
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise helper.timeout_expired(timeout or 0.0)
            health_wait = FORWARD_HEALTH_INTERVAL if self._forwards.has_forwards else remaining
            wait_timeout = (
                health_wait if remaining is None else min(remaining, health_wait or remaining)
            )
            helper.wait_for_change(wait_timeout)

    def close(self) -> None:
        if self.closed:
            return
        forward_errors: list[BaseException] = []
        try:
            self._forwards.close()
        except BaseException as error:
            forward_errors.append(error)

        helper_error: BaseException | None = None
        helper_cleanup_errors: tuple[BaseException, ...] = ()
        if self._helper is not None:
            try:
                helper_result = self._helper.close()
                helper_error = helper_result.error
                helper_cleanup_errors = helper_result.cleanup_errors
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

    def _helper_or_error(self) -> _HelperClient:
        if self._helper is None:
            raise SessionError("remote helper control session is not open")
        return self._helper
