# SPDX-License-Identifier: Apache-2.0

"""Local session coordination, failure publication, and cleanup ownership."""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from collections.abc import Callable, Iterable
from typing import Protocol

from zephyr_remote_openocd.controller._cleanup import add_failure_note
from zephyr_remote_openocd.controller._deployment import DeploymentResult, deploy_helper
from zephyr_remote_openocd.controller._forwarding import (
    ForwardAdvisory,
    ForwardFailure,
    ForwardManager,
    ForwardStartError,
)
from zephyr_remote_openocd.controller._ssh import SshCommand
from zephyr_remote_openocd.controller.authority import (
    SessionAuthority,
    SessionClosedError,
    SessionError,
)
from zephyr_remote_openocd.controller.cancellation import admit_effect, current_operation
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.plan import SessionAllocation, SessionDescriptor, SessionPlan
from zephyr_remote_openocd.controller.preferred_address_cache import (
    load_preferred_address,
    remember_preferred_address,
)
from zephyr_remote_openocd.controller.staging import build_archive
from zephyr_remote_openocd.controller.staging_spec import StagedEntry
from zephyr_remote_openocd.controller.waiting import (
    Notifier,
    ProcessHandle,
    WaitSources,
    wait_for,
)
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
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
        advisory.forward.name,
        advisory.phase,
        advisory.forward.local_port,
        advisory.failure,
    )


def _report_cleanup_after_cancellation() -> None:
    _LOGGER.warning("Interrupted; cleaning up the remote session")


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
        listeners: Iterable[ListenerSpec],
        *,
        preferred_address: str | None = None,
    ) -> str | None: ...

    def observe_status(self) -> None: ...

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired: ...

    def close(self) -> None: ...


class SessionForwards(Protocol):
    """Forwarding operations owned by ``SessionController``."""

    @property
    def forwards(self) -> tuple[ForwardSpec, ...]: ...

    def live_processes(self) -> tuple[ProcessHandle, ...]: ...

    def start(self, forwards: Iterable[ForwardSpec], remote_address: str) -> None: ...

    def check_health(self, report: Callable[[ForwardFailure], None]) -> None: ...

    def close(self) -> None: ...


class SessionController:
    """Coordinate one helper connection and its SSH forwarding processes."""

    def __init__(
        self,
        plan: SessionPlan,
        deployment: DeploymentResult,
        *,
        advisory_handler: Callable[[ForwardAdvisory], None] | None = None,
        cancellation_handler: Callable[[], None] | None = None,
    ) -> None:
        self.plan = plan
        self.deployment = deployment
        self._forwards: SessionForwards = ForwardManager(plan.ssh_command, plan.host)
        self._helper_connection: SessionConnection | None = None
        # Resource-free until acquisition opens it inside the caller's cleanup scope.
        self._notifier = Notifier()
        self._authority = SessionAuthority(notify=self._notifier.notify)
        self.closed = False
        self.descriptor: SessionDescriptor | None = None
        self._advisory_handler = advisory_handler or _log_forward_advisory
        self._cancellation_handler = cancellation_handler or _report_cleanup_after_cancellation
        self._required_forwards: set[ForwardSpec] = set()
        self._warned_forwards: set[ForwardSpec] = set()

    @property
    def forwarded_specs(self) -> tuple[ForwardSpec, ...]:
        return self._forwards.forwards

    @property
    def openocd_returncode(self) -> int | None:
        terminal = self._authority.snapshot().terminal
        result = None if terminal is None else terminal.outcome.child_result
        return None if result is None else result.returncode

    @classmethod
    def prepare(
        cls,
        plan: SessionPlan,
        *,
        advisory_handler: Callable[[ForwardAdvisory], None] | None = None,
        cancellation_handler: Callable[[], None] | None = None,
    ) -> SessionController:
        """Create the coordinator without acquiring live session resources."""
        deployment = deploy_helper(plan.ssh_command, plan.host)
        return cls(
            plan,
            deployment,
            advisory_handler=advisory_handler,
            cancellation_handler=cancellation_handler,
        )

    def acquire(
        self,
        *,
        output_handler: Callable[[str, str, bool], None] | None = None,
        process_start_handler: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        """Acquire resources on an owned coordinator; the caller must close it."""
        if self.closed or self._helper_connection is not None:
            raise SessionError("remote session has already been acquired or closed")
        self._notifier.open()
        self._helper_connection = HelperConnection(
            self.plan.ssh_command,
            self.plan.host,
            self.deployment,
            output_handler=output_handler,
            process_start_handler=process_start_handler,
            authority=self._authority,
            notifier=self._notifier,
        )
        try:
            self._helper_connection.acquire()
            self._stage(self.plan.staged_files)
            self._start_process(self.plan.forwards)
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def _stage(self, files: Iterable[StagedEntry]) -> None:
        connection = self._connection_or_error()
        archive = build_archive(files)
        primary: BaseException | None = None
        try:
            command = (
                f"python3 {shlex.quote(self.deployment.path)} stage "
                f"{shlex.quote(connection.allocation.remote_workspace)}"
            )
            result = self.plan.ssh_command.run_stream(
                self.plan.host, command, archive.stream, timeout=60
            )
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                archive.stream.close()
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                add_failure_note(primary, "staging archive cleanup also failed", cleanup_error)
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
        except (ProtocolError, ValueError) as error:
            raise SessionError(f"invalid remote staging response: {result.stdout!r}") from error

    def _start_process(self, forwards: Iterable[ForwardSpec]) -> SessionDescriptor | None:
        forward_specs = tuple(forwards)
        connection = self._connection_or_error()
        address = connection.start_process(
            self.plan.process,
            self.plan.listeners,
            preferred_address=load_preferred_address(self.plan.host, self.plan.ssh_command)
            if self.plan.listeners
            else None,
        )
        if address is None:
            return None
        self._authority.record_ready()
        self.descriptor = SessionDescriptor(connection.allocation, address)
        auxiliary = set(self.plan.auxiliary_forwards)
        self.forward(tuple(forward for forward in forward_specs if forward not in auxiliary))
        self.forward(self.plan.auxiliary_forwards, required=False)
        returncode = self.poll_operation()
        if forward_specs and returncode is not None:
            raise SessionError(f"remote OpenOCD exited during startup with status {returncode}")
        return self.descriptor

    def forward(self, forwards: Iterable[ForwardSpec], *, required: bool = True) -> None:
        """Commit forwarding entry through the shared session authority."""
        try:
            self._forward(forwards, required=required)
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def _forward(self, forwards: Iterable[ForwardSpec], *, required: bool) -> None:
        if self.closed:
            raise SessionClosedError("remote session is closed")
        if self.descriptor is None:
            raise SessionError("remote session is not ready for additional forwarding")
        forward_specs = tuple(forwards)
        if required:
            self._admit_dependent_work()
            if not forward_specs:
                return
            self._start_forward(forward_specs, self.descriptor.remote_address, required=True)
            self._required_forwards.update(forward_specs)
            remember_preferred_address(
                self.plan.host, self.plan.ssh_command, self.descriptor.remote_address
            )
        elif not required:
            for forward in forward_specs:
                self._admit_dependent_work()
                self._start_forward((forward,), self.descriptor.remote_address, required=False)

    def _start_forward(
        self, forwards: tuple[ForwardSpec, ...], remote_address: str, *, required: bool
    ) -> None:
        try:
            self._forwards.start(forwards, remote_address)
        except BaseException as error:
            if not required and isinstance(error, ForwardStartError) and not error.cleanup_errors:
                primary = self._authority.resolve_optional_failure(error)
                if primary is None:
                    self._advisory_handler(ForwardAdvisory(error.forward, "startup", error))
                    return
            else:
                primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def make_forwards_optional(self, forwards: Iterable[ForwardSpec]) -> None:
        """Reclassify owned forwards after their required phase completes."""
        if self.closed or self._authority.snapshot().closing:
            raise SessionClosedError("remote session is closed")
        forward_set = set(forwards)
        if not forward_set.issubset(self._forwards.forwards):
            raise SessionError("only owned forwards can become best-effort")
        self._required_forwards.difference_update(forward_set)

    def _check_forward_health(self) -> None:
        self._forwards.check_health(self._record_forward_failure)

    def _record_forward_failure(self, failure: ForwardFailure) -> None:
        forward = failure.forward
        error = failure.as_error()
        if forward in self._required_forwards:
            self._authority.record_failure("FORWARD_FAILURE", error, key=("forward", forward))
        elif forward not in self._warned_forwards:
            self._advisory_handler(ForwardAdvisory(forward, "runtime", error))
            self._warned_forwards.add(forward)

    def _observe_health(self) -> None:
        connection = self._connection_or_error()
        for observe in (connection.observe_status, self._check_forward_health):
            try:
                observe()
            except BaseException as error:
                self._authority.record_failure("HEALTH_OBSERVATION_FAILURE", error)

    def poll_operation(self) -> int | None:
        """Observe transport/forward health and project the authority's operation result."""
        if not self.closed:
            self._observe_health()
        return self._authority.operation_status()

    def _admit_dependent_work(self) -> None:
        """Commit entry; the operation's cancellation read is its final condition."""
        self._authority.admit_work(
            self._required_forwards, self._forwards.forwards, final_admission=admit_effect
        )

    def run_local_client[ResultT](self, launch: Callable[[], ResultT]) -> ResultT:
        """Commit actual execution entry after accounting recorded fatal facts."""
        try:
            return self._enter_local_client(launch)
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def run_interactive_client[ResultT](self, launch: Callable[[], ResultT]) -> ResultT:
        """Launch a client that owns terminal Ctrl-C for its whole invocation.

        Ownership transfers before entry is revalidated, so a cancellation
        latched before the transfer still refuses the launch, and Ctrl-C after
        it belongs to the client and its wrappers. Runner capture is restored
        before the caller can admit further work.
        """
        operation = current_operation()
        try:
            if operation is None:
                return self._enter_local_client(launch)
            with operation.interactive_client():
                return self._enter_local_client(launch)
        except BaseException as error:
            primary = self._authority.resolve_effect_failure(error)
            raise primary from primary.__cause__

    def _enter_local_client[ResultT](self, launch: Callable[[], ResultT]) -> ResultT:
        returncode = self.poll_operation()
        if returncode is not None:
            raise SessionError(f"remote OpenOCD exited before dependent launch ({returncode})")
        self._admit_dependent_work()
        return launch()

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

    def observation_sources(self) -> WaitSources:
        """Helper facts arrive through its reader; forwards retain exit wakeups."""
        return WaitSources(notifiers=(self._notifier,), processes=self._forwards.live_processes())

    def wait_for_openocd_exit(self, timeout: float | None = None) -> int:
        if self.closed:
            result = self.poll_operation()
            if result is None:
                raise SessionClosedError("remote session closed without a natural OpenOCD exit")
            return result
        connection = self._connection_or_error()
        deadline = None if timeout is None else time.monotonic() + timeout
        expired = False
        while True:
            result = self.poll_operation()
            if result is not None:
                return result
            # Expiry is reported only after the common wait has dispatched any
            # latched cancellation and the session has been observed again.
            if expired:
                raise self._authority.resolve_effect_failure(
                    connection.timeout_expired(timeout or 0.0)
                )
            try:
                expired = wait_for(sources=self.observation_sources(), deadline=deadline).expired
            except BaseException as error:
                primary = self._authority.resolve_effect_failure(error)
                raise primary from primary.__cause__

    def close(self) -> None:
        if self.closed:
            return
        self._begin_closure()
        self._authority.cancel()
        # Sample before disposal erases already-exited SSH processes.
        if self._helper_connection is not None:
            self._observe_health()
        for cleanup in (self._forwards.close, self._close_connection, self._notifier.close):
            try:
                cleanup()
            except BaseException as error:
                self._authority.record_failure("SESSION_CLEANUP_FAILURE", error)
        self.closed = True
        failure = self._authority.take_unreported_failure()
        if failure is not None:
            raise failure

    def _begin_closure(self) -> None:
        """Enter settlement; closure is the boundary for a still-latched Ctrl-C.

        Settlement then attempts every cleanup without dispatching further
        cancellation, so repeated Ctrl-C cannot skip or shorten it.
        """
        operation = current_operation()
        if operation is None or not operation.begin_closure():
            return
        self._authority.record_failure("OPERATION_FAILURE", operation.cancellation)
        try:
            self._cancellation_handler()
        except BaseException as error:
            self._authority.record_failure("CLEANUP_STATUS_FAILURE", error)

    def _close_connection(self) -> None:
        if self._helper_connection is None:
            return
        self._helper_connection.close()

    def _connection_or_error(self) -> SessionConnection:
        if self._helper_connection is None:
            raise SessionError("remote helper control session is not open")
        return self._helper_connection
