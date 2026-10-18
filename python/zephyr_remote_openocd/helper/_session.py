# SPDX-License-Identifier: Apache-2.0

"""Coordinate helper resources through one semantic lifecycle authority."""

from __future__ import annotations

import asyncio
import signal
import socket
import sys
from collections.abc import Coroutine, Iterable
from pathlib import Path
from types import FrameType
from typing import IO, Any, Literal

from zephyr_remote_openocd.helper import _addresses as addresses
from zephyr_remote_openocd.helper import _process as process
from zephyr_remote_openocd.helper import _streams as streams
from zephyr_remote_openocd.helper import _workspace as workspace
from zephyr_remote_openocd.helper._cleanup import raise_cleanup_errors
from zephyr_remote_openocd.helper._observations import (
    ChildExited,
    ChildOutput,
    ControlEOF,
    Deadline,
    GroupCleaned,
    Observation,
    ObservationFailed,
    SignalReceived,
    StartReceived,
)
from zephyr_remote_openocd.helper._signals import SessionSignals
from zephyr_remote_openocd.helper.lifecycle import (
    AuthorizedAttempt,
    Closed,
    Created,
    LifecycleAuthority,
    OwnedAttempt,
    Starting,
    Terminating,
)
from zephyr_remote_openocd.protocol.arguments import materialize_argv
from zephyr_remote_openocd.protocol.commands import StartRequest, decode_command, validate_argv
from zephyr_remote_openocd.protocol.outcome import (
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    ResidualResource,
    Trigger,
)
from zephyr_remote_openocd.protocol.wire import ControlFrameBuffer, terminal_fields

MAX_PENDING_OBSERVATIONS = 64


class HelperSession:
    """Coordinate physical facts through the canonical remote lifecycle."""

    def __init__(self, session_id: str, work: Path, workspace_lock: IO[bytes]) -> None:
        self.session_id = session_id
        self.work = work
        self.workspace_lock = workspace_lock
        self.lifecycle: LifecycleAuthority[StartRequest] = LifecycleAuthority()
        self.request: StartRequest | None = None
        self.child: process.SupervisedProcess | None = None
        self._child_acquisition: process.ChildAcquisition | None = None
        self._child_settlement: process.ChildSettlement | None = None
        self._child_acquired = False
        self._address_lease: socket.socket | None = None
        self.address = ""
        self.cleanup_errors: list[BaseException] = []
        self._events: asyncio.Queue[Observation] = asyncio.Queue(MAX_PENDING_OBSERVATIONS)
        self._signals: asyncio.Queue[int] = asyncio.Queue(1)
        self._signal_scope = SessionSignals()
        self._signal_accounted = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: asyncio.TaskGroup | None = None
        self._session_tasks: list[asyncio.Task[ObservationFailed | None]] = []
        self._observation_failures: dict[asyncio.Task[Any], ObservationFailed] = {}
        self._deadline_task: asyncio.Task[ObservationFailed | None] | None = None
        self._child_cleanup: asyncio.Task[ObservationFailed | None] | GroupCleaned | None = None
        self._attempt_finished = False
        self._staging_closed = False
        self._resources_released = False
        self._output_available = True

    @classmethod
    def create(cls) -> HelperSession:
        session_id, work, workspace_lock = workspace.new_workspace()
        try:
            return cls(session_id, work, workspace_lock)
        except BaseException as failure:
            for cleanup in (lambda: workspace.remove_workspace(work), workspace_lock.close):
                try:
                    cleanup()
                except BaseException as exc:
                    failure.add_note(f"workspace adoption cleanup also failed: {exc}")
                    for note in getattr(exc, "__notes__", ()):
                        failure.add_note(f"workspace adoption cleanup detail: {note}")
            raise

    def announce(self) -> None:
        streams.emit(
            "SESSION_CREATED",
            helper="zephyr_remote_openocd",
            session_id=self.session_id,
            remote_workspace=str(self.work),
        )

    def handle_signal(self, signum: int = signal.SIGTERM, _frame: FrameType | None = None) -> None:
        if self._signal_scope.capture(signum) and self._loop is not None:
            self._loop.call_soon_threadsafe(self._enqueue_signal, signum)

    def _enqueue_signal(self, signum: int) -> None:
        if self._signals.empty():
            self._signals.put_nowait(signum)

    async def _observe_signals(self) -> None:
        await self._events.put(SignalReceived(await self._signals.get()))

    async def _observe_control(self) -> None:
        frames = ControlFrameBuffer()
        started = False
        with streams.AsyncInput(sys.stdin.buffer.fileno()) as reader:
            while True:
                chunk = await reader.read()
                if not chunk:
                    frames.finish()
                    await self._events.put(ControlEOF())
                    return
                if started:
                    raise ValueError("unexpected input after START")
                frames.feed(chunk)
                frame = frames.pop_frame()
                if frame is not None:
                    request = decode_command(frame)
                    if frames.has_pending_bytes:
                        raise ValueError("unexpected input after START")
                    started = True
                    await self._events.put(StartReceived(request))

    async def _observe_output(
        self, child: process.SupervisedProcess, name: str, stream: IO[bytes]
    ) -> None:
        with streams.AsyncInput(stream.fileno()) as reader:
            child.readers[name] = reader
            while True:
                chunk = await reader.read()
                await self._events.put(ChildOutput(child, name, chunk))
                if not chunk:
                    return

    async def _observe_exit(self, child: process.SupervisedProcess) -> None:
        await self._events.put(ChildExited(child, await child.wait_for_exit()))

    async def _observe_deadline(
        self, child: process.SupervisedProcess, timeout: float, *, readiness: bool
    ) -> None:
        await asyncio.sleep(timeout)
        await self._events.put(Deadline(child, readiness))

    async def _guard(
        self,
        source: str,
        observation: Coroutine[Any, Any, None],
        child: process.SupervisedProcess | None = None,
    ) -> ObservationFailed | None:
        try:
            await observation
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = ObservationFailed(source, exc, child)
            task = asyncio.current_task()
            assert task is not None
            self._observation_failures[task] = failure
            if task.cancelling():
                return failure
            await self._events.put(failure)
        return None

    def _observe(
        self,
        source: str,
        observation: Coroutine[Any, Any, None],
        child: process.SupervisedProcess | None = None,
    ) -> asyncio.Task[ObservationFailed | None]:
        assert self._tasks is not None
        guarded = self._guard(source, observation, child)
        try:
            task = self._tasks.create_task(guarded, name=source)
        except BaseException:
            guarded.close()
            observation.close()
            raise
        task.add_done_callback(lambda _task: observation.close())
        (self._session_tasks if child is None else child.tasks).append(task)
        return task

    def _failure(self, trigger: Trigger, code: str, error: BaseException) -> None:
        self.lifecycle.terminate(trigger, Diagnostic.from_exception(code, error))

    def _cleanup_failure(self, error: BaseException) -> None:
        self.cleanup_errors.append(error)
        self._failure(Trigger.HELPER_FAILURE, "CLEANUP_FAILURE", error)

    def account_recorded_failures(self) -> None:
        if self._signal_scope.pending_signum is not None and not self._signal_accounted:
            self._signal_accounted = True
            self.lifecycle.terminate(
                Trigger.SIGNAL,
                Diagnostic(
                    "REMOTE_SIGNAL", f"helper received signal {self._signal_scope.pending_signum}"
                ),
            )
        for failure in tuple(self._observation_failures.values()):
            self._observation_failed(failure)

    def _observation_failed(self, failure: ObservationFailed) -> None:
        for task, recorded in tuple(self._observation_failures.items()):
            if recorded is failure:
                del self._observation_failures[task]
                break
        else:
            return
        if failure.child is not None and failure.child is not self.child:
            return
        if failure.child is not None and failure.source in ("stdout", "stderr"):
            failure.child.output_finished.add(failure.source)
        trigger = (
            Trigger.PROTOCOL_FAILURE
            if failure.source == "control"
            else (
                Trigger.OUTPUT_FAILURE
                if failure.source == "protocol output"
                else Trigger.HELPER_FAILURE
            )
        )
        self._failure(trigger, "OBSERVER_FAILURE", failure.exception)

    def _start_attempt(self) -> None:
        self.account_recorded_failures()
        state = self.lifecycle.state
        if not isinstance(state, Starting) or not isinstance(state.attempt, AuthorizedAttempt):
            return
        request = state.request
        allocated = addresses.allocate_listener_address(
            [listener.remote_port for listener in request.listeners],
            preferred_address=request.preferred_address,
        )
        self._address_lease = allocated.lease
        self.address = str(allocated)
        argv = materialize_argv(
            request.argv,
            workspace=str(self.work),
            address=self.address,
            literal_prefix=request.literal_prefix,
            argv_templates=request.argv_templates,
        )
        validate_argv(list(argv))
        process.check_required_paths(
            request.required_paths, workspace=str(self.work), address=self.address
        )
        self.account_recorded_failures()
        if not isinstance(self.lifecycle.state, Starting):
            return
        try:
            streams.emit("ATTEMPT", argv=list(argv))
        except BaseException as error:
            self._failure(Trigger.OUTPUT_FAILURE, "ATTEMPT_ADMISSION", error)
            return
        if not self.lifecycle.enter_attempt(admitted=True):
            return
        owner = process.ChildAcquisition()
        self._child_acquisition = owner
        try:
            (self.work / workspace.UNCONFIRMED_CHILD).touch(mode=0o600)
            self.child = process.spawn_child(
                argv,
                cwd=self.work / "staged",
                environment=process.child_environment(request),
                required_output_sentinels=request.required_output_sentinels,
                ownership=owner,
            )
        except BaseException as error:
            self._failure(Trigger.STARTUP_FAILURE, "SPAWN_FAILURE", error)
        finally:
            owner.producer_quiescent = True
            self._child_acquired |= owner.process is not None
            if self.child is None:
                self.child = owner.child
        if self.child is None:
            if owner.process is not None and owner.process.returncode is not None:
                if owner.termination_requested:
                    self.lifecycle.record_child_termination()
                self.lifecycle.observe_child_exit(owner.process.returncode)
            return
        self.child.termination_requested |= owner.termination_requested
        self.lifecycle.adopt_attempt()
        self._start_output_observers(self.child)
        self._observe("child exit", self._observe_exit(self.child), self.child)
        if request.completion_policy == CompletionPolicy.LIVE_SERVER and isinstance(
            self.lifecycle.state, Starting
        ):
            self._deadline_task = self._observe(
                "readiness deadline",
                self._observe_deadline(self.child, request.readiness_timeout, readiness=True),
                self.child,
            )
            self._ready()

    def _start_output_observers(self, child: process.SupervisedProcess) -> None:
        observed = {task.get_name() for task in child.tasks}
        errors = []
        for name in ("stdout", "stderr"):
            if name in observed or name in child.output_finished:
                continue
            try:
                stream = child.process.stdout if name == "stdout" else child.process.stderr
                if stream is None:
                    raise RuntimeError("child output was not captured")
                if stream.closed:
                    child.output_finished.add(name)
                else:
                    self._observe(name, self._observe_output(child, name, stream), child)
            except BaseException as error:
                # No observer can report this stream's EOF. Its pipe remains
                # owned by the child and is closed during attempt settlement.
                child.output_finished.add(name)
                errors.append(error)
        raise_cleanup_errors(errors)

    def _record_exit(self, child: process.SupervisedProcess, returncode: int) -> None:
        if child.termination_requested:
            self.lifecycle.record_child_termination()
        observed = self.lifecycle.observe_child_exit(returncode)
        state = self.lifecycle.state
        if (
            observed
            and isinstance(state, Starting)
            and state.request.completion_policy == CompletionPolicy.LIVE_SERVER
        ):
            # Even a genuine bind failure can follow opaque Tcl and target
            # initialization. Child status and output cannot certify replay.
            self.lifecycle.terminate(
                Trigger.STARTUP_FAILURE,
                Diagnostic("STARTUP_EXIT", f"child exited before READY with status {returncode}"),
            )

    def _ready(self) -> None:
        self.account_recorded_failures()
        state = self.lifecycle.state
        child = self.child
        if (
            not isinstance(state, Starting)
            or child is None
            or state.request.completion_policy != CompletionPolicy.LIVE_SERVER
        ):
            return
        for marker in set(state.request.required_output_sentinels) - set(
            child.required_output_sentinels.unseen
        ):
            self.lifecycle.observe_marker(marker)
        state = self.lifecycle.state
        if (
            not isinstance(state, Starting)
            or not isinstance(state.attempt, OwnedAttempt)
            or state.attempt.child_result is not None
            or not set(state.request.required_output_sentinels).issubset(state.evidence)
        ):
            return
        returncode = child.poll()
        if returncode is not None:
            self._record_exit(child, returncode)
            return
        with self._signal_scope.commit():
            self.account_recorded_failures()
            if not isinstance(self.lifecycle.state, Starting):
                return
            try:
                streams.emit(
                    "READY",
                    remote_address=self.address,
                    child_pid=child.pid,
                )
            except BaseException as error:
                self._failure(Trigger.OUTPUT_FAILURE, "READY_ADMISSION", error)
                return
            self.lifecycle.ready(child_live=True, admitted=True)
        if self._deadline_task is not None:
            self._deadline_task.cancel()

    def _output(self, observation: ChildOutput) -> None:
        child = observation.child
        if child is not self.child:
            return
        for fragment in child.decoders[observation.stream].feed(observation.chunk):
            if self._output_available:
                try:
                    streams.emit(
                        "CHILD_OUTPUT",
                        stream=fragment.stream,
                        payload=fragment.payload,
                        line_end=fragment.line_end,
                    )
                except BaseException as error:
                    self._output_available = False
                    self._failure(Trigger.OUTPUT_FAILURE, "CHILD_OUTPUT_ADMISSION", error)
        if not observation.chunk:
            child.output_finished.add(observation.stream)
        self._ready()

    async def _observe_final_readiness(self, child: process.SupervisedProcess) -> None:
        readers = list(child.readers.values())
        if readers:
            await asyncio.wait([reader.checkpoint() for reader in readers])
        await self._events.put(Deadline(child, readiness=True, final=True))

    @staticmethod
    async def _dispose_group(child: process.SupervisedProcess) -> GroupCleaned:
        failure = None
        try:
            await child.terminate()
        except BaseException as error:
            failure = error
        return GroupCleaned(child, failure)

    async def _clean_group(self, child: process.SupervisedProcess) -> None:
        await self._events.put(await self._dispose_group(child))

    async def _begin_child_cleanup(self) -> None:
        child = self.child
        if child is None or self._child_cleanup is not None:
            return
        if self._deadline_task is not None:
            self._deadline_task.cancel()
        assert self._tasks is not None
        observation = self._clean_group(child)
        guarded = self._guard("process-group cleanup", observation, child)
        try:
            # The TaskGroup owns successful creation immediately; this slot
            # adopts it before auxiliary setup or task-list registration.
            self._child_cleanup = self._tasks.create_task(guarded, name="process-group cleanup")
        except BaseException as error:
            guarded.close()
            observation.close()
            self._cleanup_failure(error)
            # The coordinator still owns the child. Disposal does not depend
            # on allocating a replacement task or publishing to its own queue.
            self._handle(await self._dispose_group(child))
        else:
            child.tasks.append(self._child_cleanup)
            self._child_cleanup.add_done_callback(lambda _task: observation.close())
        try:
            self._start_output_observers(child)
        except BaseException as error:
            self._cleanup_failure(error)

    async def _cancel(self, tasks: Iterable[asyncio.Task[Any]]) -> None:
        owned = tuple(tasks)
        for task in owned:
            task.cancel()
        for task in owned:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException as error:
                self._cleanup_failure(error)
            failure = self._observation_failures.get(task)
            if failure is not None:
                self._observation_failed(failure)

    def _close_address_lease(self) -> None:
        if self._address_lease is not None:
            try:
                self._address_lease.close()
            except BaseException as error:
                self._cleanup_failure(error)
            else:
                self._address_lease = None

    async def _finish_attempt(self) -> None:
        child = self.child
        assert child is not None
        await self._cancel(child.tasks)
        try:
            child.close_streams()
        except BaseException as error:
            self._cleanup_failure(error)
        returncode = child.returncode
        if returncode is not None:
            self._record_exit(child, returncode)
        owner = self._child_acquisition
        assert owner is not None
        self._child_settlement = process.ChildSettlement(
            owner.producer_quiescent,
            child.group_disposed,
            all(task.done() for task in child.tasks),
            all(
                stream is None or stream.closed
                for stream in (child.process.stdout, child.process.stderr)
            ),
        )
        self._close_address_lease()
        self.account_recorded_failures()
        state = self.lifecycle.state
        if isinstance(state, Starting):
            failure = Diagnostic(
                "STARTUP_EXIT", f"child exited before READY with status {returncode}"
            )
            self.lifecycle.terminate(Trigger.STARTUP_FAILURE, failure)
        if self._child_settlement.confirmed and self._address_lease is None:
            self.lifecycle.settle_attempt(producer_quiescent=True, resources_disposed=True)
            self.child = None
        else:
            self._cleanup_failure(RuntimeError("child disposal remains unconfirmed"))
        self._attempt_finished = True

    def _handle(self, observation: Observation) -> None:
        if isinstance(observation, StartReceived):
            self.account_recorded_failures()
            if isinstance(self.lifecycle.state, Created):
                self.request = observation.request
                self.lifecycle.start(observation.request)
                try:
                    self._start_attempt()
                except BaseException as error:
                    self._failure(Trigger.STARTUP_FAILURE, "STARTUP_FAILURE", error)
        elif isinstance(observation, ControlEOF):
            self.lifecycle.terminate(Trigger.CONTROLLER_EOF)
        elif isinstance(observation, SignalReceived):
            self.account_recorded_failures()
        elif isinstance(observation, ObservationFailed):
            self._observation_failed(observation)
        elif isinstance(observation, ChildOutput):
            self._output(observation)
        elif isinstance(observation, ChildExited):
            if observation.child is self.child:
                self._record_exit(observation.child, observation.returncode)
        elif isinstance(observation, GroupCleaned):
            if observation.child is self.child:
                self._child_cleanup = observation
                if observation.exception is not None:
                    self._cleanup_failure(observation.exception)
                if observation.child.output_finished != {"stdout", "stderr"}:
                    try:
                        self._deadline_task = self._observe(
                            "output drain deadline",
                            self._observe_deadline(
                                observation.child, streams.CHILD_RELAY_JOIN_TIMEOUT, readiness=False
                            ),
                            observation.child,
                        )
                    except BaseException as error:
                        self._cleanup_failure(error)
                        observation.child.output_finished.update(("stdout", "stderr"))
        elif isinstance(observation, Deadline) and observation.child is self.child:
            state = self.lifecycle.state
            if observation.readiness and isinstance(state, Starting):
                if observation.final:
                    self._ready()
                    state = self.lifecycle.state
                    if (
                        isinstance(state, Starting)
                        and isinstance(state.attempt, OwnedAttempt)
                        and state.attempt.child_result is None
                    ):
                        self._failure(
                            Trigger.STARTUP_FAILURE,
                            "READINESS_TIMEOUT",
                            RuntimeError("process readiness timed out"),
                        )
                else:
                    self._observe(
                        "final readiness observation",
                        self._observe_final_readiness(observation.child),
                        observation.child,
                    )
            elif not observation.readiness and observation.child.output_finished != {
                "stdout",
                "stderr",
            }:
                self._cleanup_failure(RuntimeError("child output relay did not stop"))
                observation.child.output_finished.update(("stdout", "stderr"))

    async def _coordinate(self) -> None:
        while True:
            self.account_recorded_failures()
            state = self.lifecycle.state
            if isinstance(state, Terminating):
                if not self._staging_closed:
                    try:
                        workspace.close_staging_admission(self.work)
                    except BaseException as error:
                        self._cleanup_failure(error)
                    self._staging_closed = True
                if self.child is None or self._attempt_finished:
                    return
                await self._begin_child_cleanup()
            elif (
                isinstance(state, Starting)
                and isinstance(state.attempt, OwnedAttempt)
                and state.attempt.child_result is not None
            ):
                await self._begin_child_cleanup()
            if (
                isinstance(self._child_cleanup, GroupCleaned)
                and self.child is not None
                and self.child.output_finished == {"stdout", "stderr"}
            ):
                await self._finish_attempt()
                continue
            self._handle(await self._events.get())
            await asyncio.sleep(0)

    def _child_disposed(self) -> bool:
        owner = self._child_acquisition
        return (
            owner is None
            or owner.producer_quiescent
            and (
                owner.process is None
                or owner.rollback_confirmed
                or self._child_settlement is not None
                and self._child_settlement.confirmed
            )
        )

    def release_workspace(self) -> None:
        if self._resources_released:
            return
        self._resources_released = True
        self._close_address_lease()
        try:
            if self._child_disposed() and self._address_lease is None:
                try:
                    (self.work / workspace.UNCONFIRMED_CHILD).unlink(missing_ok=True)
                except BaseException as error:
                    self._cleanup_failure(error)
                workspace.remove_workspace(self.work)
            else:
                workspace.close_staging_admission(self.work)
                self._cleanup_failure(
                    RuntimeError("retaining workspace for unconfirmed child disposal")
                )
        except BaseException as error:
            self._cleanup_failure(error)
        try:
            self.workspace_lock.close()
        except BaseException as error:
            self._cleanup_failure(error)

    def _disposed_path(self, path: Path) -> bool:
        try:
            return workspace.path_absent(path)
        except BaseException as failure:
            self._cleanup_failure(failure)
            return False

    def _cleanup_report(self) -> CleanupReport:
        residuals: list[ResidualResource] = []
        owner = self._child_acquisition
        if owner is not None and not owner.producer_quiescent:
            residuals.append("child_producer")
        if not self._child_disposed():
            if (
                owner is not None
                and owner.child is None
                or self._child_settlement is None
                or not self._child_settlement.group_disposed
            ):
                residuals.append("child_group")
            if self._child_settlement is None or not (
                self._child_settlement.observers_settled and self._child_settlement.pipes_closed
            ):
                residuals.append("child_relays")
        if self._address_lease is not None:
            residuals.append("address_lease")
        child_status: Literal["not_acquired", "confirmed", "unconfirmed"] = (
            "unconfirmed"
            if residuals
            else ("confirmed" if self._child_acquired else "not_acquired")
        )
        if not self._disposed_path(self.work):
            residuals.append("workspace")
        if not self.workspace_lock.closed or not all(
            self._disposed_path(path)
            for path in (workspace.lease_path(self.work), workspace.closure_path(self.work))
        ):
            residuals.append("workspace_metadata")
        workspace_status: Literal["not_created", "confirmed", "unconfirmed"] = (
            "unconfirmed" if {"workspace", "workspace_metadata"} & set(residuals) else "confirmed"
        )
        if self._child_disposed() and self._address_lease is None and owner is not None:
            self.lifecycle.settle_attempt(
                producer_quiescent=owner.producer_quiescent,
                resources_disposed=True,
            )
        return CleanupReport(child_status, workspace_status, tuple(residuals))

    async def _run_session_tasks(
        self, output: streams.ProtocolOutput, signals: SessionSignals
    ) -> None:
        writer_task = None
        async with asyncio.TaskGroup() as tasks:
            self._tasks = tasks
            try:
                if output.descriptor is not None:
                    writer_task = self._observe("protocol output", output.run())
                try:
                    self.announce()
                except BaseException as error:
                    self._failure(Trigger.OUTPUT_FAILURE, "SESSION_CREATED_ADMISSION", error)
                signals.install(self.handle_signal)
                self._observe("control", self._observe_control())
                self._observe("signal", self._observe_signals())
                await self._coordinate()
            except BaseException as error:
                self._failure(Trigger.HELPER_FAILURE, "HELPER_FAILURE", error)
                await self._coordinate()
            finally:
                await self._cancel(task for task in self._session_tasks if task is not writer_task)
                self.release_workspace()
            cleanup = self._cleanup_report()
            with signals.commit():
                self.account_recorded_failures()
                snapshot = self.lifecycle.freeze(cleanup)
            try:
                streams.emit("SESSION_ENDED", **terminal_fields(snapshot))
                await output.drain()
            except BaseException as error:
                self._failure(Trigger.OUTPUT_FAILURE, "TERMINAL_DELIVERY", error)
            finally:
                if writer_task is not None:
                    await self._cancel((writer_task,))

    async def run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._signal_scope.pending_signum is not None:
            self._loop.call_soon(self._enqueue_signal, self._signal_scope.pending_signum)
        output = None
        token = None
        try:
            output = streams.ProtocolOutput()
            token = streams.protocol_output.set(output)
            await self._run_session_tasks(output, self._signal_scope)
        except BaseException as error:
            self._failure(Trigger.HELPER_FAILURE, "HELPER_FAILURE", error)
        finally:
            self._tasks = None
            self._loop = None
            self.release_workspace()
            if token is not None:
                streams.protocol_output.reset(token)
            if output is not None:
                try:
                    output.close()
                except BaseException as error:
                    self._cleanup_failure(error)
            for restoration_error in self._signal_scope.restore():
                self._cleanup_failure(restoration_error)
            self.account_recorded_failures()
        state = self.lifecycle.state
        if (
            not isinstance(state, Closed)
            or state.local_diagnostics
            or state.snapshot.operation_failed(
                self.request.completion_policy
                if self.request is not None
                else CompletionPolicy.LIVE_SERVER
            )
        ):
            raise SystemExit(1)

    def run(self) -> None:
        with asyncio.Runner() as runner:
            runner.run(self.run_async())
