# SPDX-License-Identifier: Apache-2.0

"""Production SSH/helper implementation of a remote session."""

from __future__ import annotations

import logging
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from typing import Protocol

from zephyr_remote_openocd.controller.authority import (
    SessionAuthority,
    SessionClosedError,
    SessionError,
)
from zephyr_remote_openocd.controller.deployment import DeploymentResult, deploy_helper
from zephyr_remote_openocd.controller.forwarding import (
    FORWARD_HEALTH_INTERVAL,
    ForwardAdvisory,
    ForwardFailure,
    ForwardManager,
    ForwardStartError,
)
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.model import (
    Service,
    SessionAllocation,
    SessionDescriptor,
    SessionPlan,
    StagedEntry,
)
from zephyr_remote_openocd.controller.preferred_address_cache import (
    load_preferred_address,
    remember_preferred_address,
)
from zephyr_remote_openocd.controller.ssh import SshCommand
from zephyr_remote_openocd.controller.staging import build_archive
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.responses import (
    validate_error_response,
    validate_openocd_version_response,
    validate_staged_response,
)
from zephyr_remote_openocd.protocol.wire import ProtocolError, decode_frame

_LOGGER = logging.getLogger(__name__)


def _log_forward_advisory(advisory: ForwardAdvisory) -> None:
    _LOGGER.warning(
        "Best-effort %s forwarding %s failure on 127.0.0.1:%s: %s",
        advisory.service.name,
        advisory.phase,
        advisory.service.local_port,
        advisory.failure,
    )


def _one_shot_failure_detail(stdout: bytes, stderr: bytes) -> str:
    """Decode a helper ERROR frame, falling back to transport diagnostics."""
    if stdout:
        try:
            message = decode_frame(stdout)
            validate_error_response(message)
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
        message = decode_frame(result.stdout)
        validate_openocd_version_response(message)
        return message["output"]
    except (KeyError, ProtocolError, ValueError) as error:
        raise SessionError(f"invalid remote OpenOCD version response: {result.stdout!r}") from error


class SessionConnection(Protocol):
    """Helper-control operations owned by ``SessionController``."""

    def acquire(self) -> None: ...

    @property
    def allocation(self) -> SessionAllocation: ...

    def start_process(
        self,
        process: ProcessSpec,
        services: Iterable[Service],
        *,
        preferred_address: str | None = None,
    ) -> str | None: ...

    def observe_status(self) -> None: ...

    def wait_for_change(self, timeout: float | None) -> None: ...

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired: ...

    def close(self) -> None: ...


class SessionForwards(Protocol):
    """Forwarding operations owned by ``SessionController``."""

    @property
    def has_forwards(self) -> bool: ...

    @property
    def services(self) -> tuple[Service, ...]: ...

    def start(self, services: Iterable[Service], remote_address: str) -> None: ...

    def check_health(self, report: Callable[[ForwardFailure], None]) -> None: ...

    def close(self) -> None: ...


class SessionController:
    """Coordinate one helper client and its SSH forwarding processes."""

    def __init__(
        self,
        request: SessionPlan,
        deployment: DeploymentResult,
        *,
        advisory_handler: Callable[[ForwardAdvisory], None] | None = None,
    ) -> None:
        self.request = request
        self.deployment = deployment
        self._forwards: SessionForwards = ForwardManager(request.ssh_command, request.host)
        self._helper: SessionConnection | None = None
        self._authority = SessionAuthority()
        self.closed = False
        self.descriptor: SessionDescriptor | None = None
        self._advisory_handler = advisory_handler or _log_forward_advisory
        self._required_services: set[Service] = set()
        self._warned_services: set[Service] = set()

    @property
    def forwarded_services(self) -> tuple[Service, ...]:
        return self._forwards.services

    @property
    def openocd_returncode(self) -> int | None:
        ending = self._authority.snapshot().ending
        result = None if ending is None else ending.outcome.child_result
        return None if result is None else result.returncode

    @classmethod
    def prepare(
        cls,
        request: SessionPlan,
        *,
        advisory_handler: Callable[[ForwardAdvisory], None] | None = None,
    ) -> SessionController:
        """Create the coordinator without acquiring live session resources."""
        deployment = deploy_helper(request.ssh_command, request.host)
        return cls(request, deployment, advisory_handler=advisory_handler)

    def acquire(
        self,
        *,
        output_handler: Callable[[str, str, bool], None] | None = None,
        process_start_handler: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        """Acquire resources on an owned coordinator; the caller must close it."""
        if self.closed or self._helper is not None:
            raise SessionError("remote session has already been acquired or closed")
        self._helper = HelperConnection(
            self.request.ssh_command,
            self.request.host,
            self.deployment,
            output_handler=output_handler,
            process_start_handler=process_start_handler,
            authority=self._authority,
        )
        try:
            self._helper.acquire()
            self._stage(self.request.staged_files)
            self._start_process(self.request.services)
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

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
            message = decode_frame(result.stdout)
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

    def _start_process(self, services: Iterable[Service]) -> SessionDescriptor | None:
        service_list = tuple(services)
        helper = self._helper_or_error()
        address = helper.start_process(
            self.request.process,
            (*service_list, *self.request.reserved_services),
            preferred_address=load_preferred_address(self.request.host, self.request.ssh_command)
            if self.request.address_services
            else None,
        )
        if address is None:
            return None
        self._authority.record_ready()
        self.descriptor = SessionDescriptor(helper.allocation, address)
        auxiliary = set(self.request.auxiliary_services)
        self.forward(tuple(service for service in service_list if service not in auxiliary))
        self.forward(self.request.auxiliary_services, required=False)
        returncode = self.poll_operation()
        if service_list and returncode is not None:
            raise SessionError(f"remote OpenOCD exited during startup with status {returncode}")
        return self.descriptor

    def forward(self, services: Iterable[Service], *, required: bool = True) -> None:
        """Commit forwarding entry through the shared session authority."""
        try:
            self._forward(services, required=required)
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def _forward(self, services: Iterable[Service], *, required: bool) -> None:
        if self.closed:
            raise SessionClosedError("remote session is closed")
        if self.descriptor is None:
            raise SessionError("remote session is not ready for additional forwarding")
        service_list = tuple(services)
        if required:
            self._authority.admit_work(self._required_services, self._forwards.services)
            if not service_list:
                return
            self._start_forward(service_list, self.descriptor.remote_address, required=True)
            self._required_services.update(service_list)
            remember_preferred_address(
                self.request.host, self.request.ssh_command, self.descriptor.remote_address
            )
        elif not required:
            for service in service_list:
                self._authority.admit_work(self._required_services, self._forwards.services)
                self._start_forward((service,), self.descriptor.remote_address, required=False)

    def _start_forward(
        self, services: tuple[Service, ...], remote_address: str, *, required: bool
    ) -> None:
        try:
            self._forwards.start(services, remote_address)
        except BaseException as error:
            if not required and isinstance(error, ForwardStartError) and not error.cleanup_errors:
                primary = self._authority.resolve_optional_failure(error)
                if primary is None:
                    self._advisory_handler(ForwardAdvisory(error.service, "startup", error))
                    return
            else:
                primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def make_forwards_optional(self, services: Iterable[Service]) -> None:
        """Reclassify owned forwards after their required phase completes."""
        if self.closed or self._authority.snapshot().closing:
            raise SessionClosedError("remote session is closed")
        service_set = set(services)
        if not service_set.issubset(self._forwards.services):
            raise SessionError("only owned forwards can become best-effort")
        self._required_services.difference_update(service_set)

    def _check_forward_health(self) -> None:
        self._forwards.check_health(self._record_forward_failure)

    def _record_forward_failure(self, failure: ForwardFailure) -> None:
        service = failure.service
        error = failure.as_error()
        if service in self._required_services:
            self._authority.record_failure("FORWARD_FAILURE", error, key=("forward", service))
        elif service not in self._warned_services:
            self._advisory_handler(ForwardAdvisory(service, "runtime", error))
            self._warned_services.add(service)

    def _observe_health(self) -> None:
        helper = self._helper_or_error()
        for observe in (helper.observe_status, self._check_forward_health):
            try:
                observe()
            except BaseException as error:
                self._authority.record_failure("HEALTH_OBSERVATION_FAILURE", error)

    def poll_operation(self) -> int | None:
        if not self.closed:
            self._observe_health()
        return self._authority.operation_status()

    def run_local_client[ResultT](self, launch: Callable[[], ResultT]) -> ResultT:
        """Commit actual execution entry after accounting recorded fatal facts."""
        try:
            returncode = self.poll_operation()
            if returncode is not None:
                raise SessionError(f"remote OpenOCD exited before dependent launch ({returncode})")
            self._authority.admit_work(self._required_services, self._forwards.services)
            return launch()
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def record_operation_failure(self, error: BaseException) -> None:
        """Report a local operation/cancellation fact to the session authority."""
        self._authority.record_failure("OPERATION_FAILURE", error)

    def finish(self) -> None:
        """Settle ownership and publish the authority's complete operation result."""
        try:
            self.close()
        except BaseException as error:
            self.record_operation_failure(error)
        failure = self._authority.publish_failure()
        if failure is not None:
            raise failure

    def wait_for_openocd_exit(self, timeout: float | None = None) -> int:
        if self.closed:
            result = self.poll_operation()
            if result is None:
                raise SessionClosedError("remote session closed without a natural OpenOCD exit")
            return result
        helper = self._helper_or_error()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self.poll_operation()
            if result is not None:
                return result
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise self._authority.resolve_effect_failure(helper.timeout_expired(timeout or 0.0))
            health_wait = FORWARD_HEALTH_INTERVAL if self._forwards.has_forwards else remaining
            wait_timeout = (
                health_wait if remaining is None else min(remaining, health_wait or remaining)
            )
            try:
                helper.wait_for_change(wait_timeout)
            except BaseException as error:
                primary = self._authority.resolve_effect_failure(error)
                raise primary from primary.__cause__

    def close(self) -> None:
        if self.closed:
            return
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            self._authority.cancel()
            # Sample before disposal erases already-exited SSH processes.
            if self._helper is not None:
                self._observe_health()
            for cleanup in (self._forwards.close, self._close_helper):
                try:
                    cleanup()
                except BaseException as error:
                    self._authority.record_failure("SESSION_CLEANUP_FAILURE", error)
            self.closed = True
        finally:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            except BaseException as error:
                self._authority.record_failure("LOCAL_CANCELLATION", error)
        failure = self._authority.take_unreported_failure()
        if failure is not None:
            raise failure

    def _close_helper(self) -> None:
        if self._helper is None:
            return
        self._helper.close()

    def _helper_or_error(self) -> SessionConnection:
        if self._helper is None:
            raise SessionError("remote helper control session is not open")
        return self._helper
