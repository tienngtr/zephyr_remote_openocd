# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import os
import select
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import suppress
from types import FrameType, SimpleNamespace, TracebackType
from typing import IO, TYPE_CHECKING, Any, Literal, Self, override

import pytest
from zephyr_remote_openocd.controller import _forwarding as forwarding_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._forwarding import ForwardManager
from zephyr_remote_openocd.controller._ssh import (
    SSH_STDERR_TAIL_BYTES,
    ManagedSshProcess,
    SshCommand,
    SshForwardSpec,
    stop_process,
)
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionController,
    SessionError,
    SessionPlan,
)
from zephyr_remote_openocd.controller.cancellation import operation_scope
from zephyr_remote_openocd.protocol.process import ProcessSpec

from tests.process_support import local_transport, stop_and_close

if TYPE_CHECKING:
    from _typeshed import TraceFunction

FORWARD_FAILURE_RC = 13
SAMPLE_FORWARD_EXIT_CODE = 9

# The Any uses in this module are confined to the subprocess boundary.
# ForwardManager owns the full ManagedSshProcess lifecycle, so introducing a
# narrower production protocol would only accommodate tests.  These doubles
# provide deterministic poll results and diagnostics; normal stderr-drain
# behavior and partial readiness input use real SshCommand processes below, and
# full cleanup ownership is covered with real managed processes in local
# integration.


class _ForwardCommand(SshCommand):
    processes: Iterator[Any]
    calls: list[tuple[str, str, SshForwardSpec | None]]

    def __init__(self, *processes: _ForwardProcess) -> None:
        super().__init__()
        object.__setattr__(self, "processes", iter(processes))
        object.__setattr__(self, "calls", [])

    @override
    def popen(
        self,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired: Callable[[ManagedSshProcess], None] | None = None,
    ) -> Any:
        self.calls.append((host, remote_command, local_forward))
        acquired_process = next(self.processes)
        if on_acquired is not None:
            on_acquired(acquired_process)
        return acquired_process


@pytest.mark.parametrize("rollback_fails", (False, True))
def test_failed_start_exposes_rollback_outcome(
    monkeypatch: pytest.MonkeyPatch, rollback_fails: bool
) -> None:
    forward = ForwardSpec("tcl", 32101, 6333)
    cleanup_error = RuntimeError("rollback cleanup failed")
    process = _ForwardProcess(None, close_stderr_error=cleanup_error if rollback_fails else None)
    manager = ForwardManager(_ForwardCommand(process), "host")
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    monkeypatch.setattr(forwarding_module, "await_ready", lambda *_args: False)
    with pytest.raises(SessionError) as raised:
        manager.start((forward,), "127.64.0.1")
    error = raised.value
    assert isinstance(error, forwarding_module.ForwardStartError)
    assert error.forward == forward
    assert forward.name in str(error)
    assert f"127.0.0.1:{forward.local_port}" in str(error)
    assert isinstance(error.cause, SessionError)
    assert error.cleanup_errors == ((cleanup_error,) if rollback_fails else ())
    assert not manager.has_forwards
    manager.close()
    assert process.terminate_calls == 1
    assert process.close_stderr_calls == 1


def test_process_creation_failure_has_service_identity_without_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cause = OSError("cannot start configured SSH client")

    class Command(_ForwardCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired: Callable[[ManagedSshProcess], None] | None = None,
        ) -> Any:
            raise cause

    forward = ForwardSpec("tcl", 32101, 6333)
    manager = ForwardManager(Command(), "host")
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    with pytest.raises(SessionError) as raised:
        manager.start((forward,), "127.64.0.1")
    assert isinstance(raised.value, forwarding_module.ForwardStartError)
    assert raised.value.forward == forward
    assert raised.value.cause is cause
    assert raised.value.cleanup_errors == ()
    assert not manager.has_forwards


@pytest.mark.timeout(60)
def test_forward_readiness_timeout_does_not_block_on_partial_output() -> None:
    # A live transport that writes a partial marker and then waits on stdin.
    code = (
        "import sys; sys.stdout.buffer.write(b'ZRO_FORWARD_'); sys.stdout.flush(); "
        "sys.stdin.buffer.read()"
    )
    process = local_transport((sys.executable, "-c", code))
    try:
        assert process.stdout is not None
        readable, _, _ = select.select((process.stdout,), (), (), 30)
        assert readable
        # The readiness deadline has already passed when the marker is partial.
        assert not forwarding_module.await_ready(process, "ZRO_FORWARD_ready", time.monotonic())
        assert process.poll() is None
    finally:
        stop_and_close(process)


def test_forward_diagnostic_keeps_a_useful_tail_after_nonzero_exit() -> None:
    code = (
        "import sys;"
        "sys.stderr.buffer.write(b'x' * 200000 + b'forward-tail\\n');"
        "sys.stderr.flush();"
        f"raise SystemExit({SAMPLE_FORWARD_EXIT_CODE})"
    )
    process = SshCommand((sys.executable, "-c", code)).popen("host", "ignored")
    try:
        assert process.wait(timeout=5) == SAMPLE_FORWARD_EXIT_CODE
        diagnostic = forwarding_module.diagnostic(process)
        assert diagnostic.endswith("forward-tail")
        assert len(diagnostic.encode()) <= SSH_STDERR_TAIL_BYTES
    finally:
        process.close_stderr()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()


def test_initial_start_forward_failure_associates_all_preflight_advisories_with_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_port, second_port = 32133, 32144
    _patch_preflight_socket(monkeypatch, {first_port, second_port})
    first = ForwardSpec("tcl", first_port, 6333)
    second = ForwardSpec("telnet", second_port, 4444)
    command = _ForwardCommand(_ForwardProcess(7))
    manager = ForwardManager(command, "host")
    try:
        with pytest.raises(SessionError) as raised:
            manager.start((first, second), "127.64.0.1")

        message = str(raised.value)
        assert f"127.0.0.1:{first_port} for tcl" in message
        assert f"127.0.0.1:{second_port} for telnet" in message
        assert command.calls[0][2] == SshForwardSpec(first_port, "127.64.0.1", 6333)
    finally:
        with suppress(BaseException):
            manager.close()


def test_dynamic_forward_failure_identifies_service_and_local_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 32155
    _patch_preflight_socket(monkeypatch, set())
    forward = ForwardSpec("rtt", port, 5555)
    manager = ForwardManager(_ForwardCommand(_ForwardProcess(9)), "host")
    try:
        with pytest.raises(SessionError) as raised:
            manager.start((forward,), "127.64.0.1")

        message = str(raised.value)
        assert forward.name in message
        assert f"127.0.0.1:{port}" in message
    finally:
        manager.close()


def test_forward_manager_rejects_a_service_already_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forward = ForwardSpec("gdb", 32177, 3333)
    command = _ForwardCommand(_ForwardProcess(None))
    manager = ForwardManager(command, "host")
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    monkeypatch.setattr(forwarding_module, "await_ready", lambda *_args: True)

    try:
        manager.start((forward,), "127.64.0.1")
        with pytest.raises(SessionError):
            manager.start((forward,), "127.64.0.1")
        assert len(command.calls) == 1
    finally:
        manager.close()


def test_failed_forward_batch_rolls_back_all_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    forwards = (ForwardSpec("gdb", 32188, 3333), ForwardSpec("tcl", 32199, 6333))
    failed_processes = (_ForwardProcess(None), _ForwardProcess(None))
    command = _ForwardCommand(*failed_processes)
    manager = ForwardManager(command, "host")
    readiness = iter((True, False))
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    monkeypatch.setattr(
        forwarding_module,
        "await_ready",
        lambda *_args: next(readiness),
    )

    with pytest.raises(SessionError):
        manager.start(forwards, "127.64.0.1")

    assert [process.terminate_calls for process in failed_processes] == [1, 1]
    assert [process.close_stderr_calls for process in failed_processes] == [1, 1]
    assert not manager.has_forwards
    manager.close()
    assert [process.terminate_calls for process in failed_processes] == [1, 1]
    assert [process.close_stderr_calls for process in failed_processes] == [1, 1]


def test_failed_forward_batch_keeps_startup_error_and_cleanup_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwards = (ForwardSpec("gdb", 32210, 3333), ForwardSpec("tcl", 32221, 6333))
    first_cleanup_error = RuntimeError("first rollback failed")
    first_stream_error = RuntimeError("first stream cleanup failed")
    second_cleanup_error = RuntimeError("second rollback failed")
    processes = (
        _ForwardProcess(
            None,
            terminate_error=first_cleanup_error,
            close_stderr_error=first_stream_error,
        ),
        _ForwardProcess(None, terminate_error=second_cleanup_error),
    )
    manager = ForwardManager(_ForwardCommand(*processes), "host")
    startup_error = KeyboardInterrupt("startup interrupted")
    readiness_calls = 0

    def await_ready(*_args: object) -> bool:
        nonlocal readiness_calls
        readiness_calls += 1
        if readiness_calls == 2:
            raise startup_error
        return True

    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    monkeypatch.setattr(forwarding_module, "await_ready", await_ready)

    with pytest.raises(KeyboardInterrupt) as raised:
        manager.start(forwards, "127.64.0.1")

    assert raised.value is startup_error
    assert [process.terminate_calls for process in processes] == [1, 1]
    assert [process.close_stderr_calls for process in processes] == [1, 1]
    notes = raised.value.__notes__
    assert any("first rollback failed" in note for note in notes)
    assert any("first stream cleanup failed" in note for note in notes)
    assert any("second rollback failed" in note for note in notes)
    assert not manager.has_forwards
    manager.close()
    assert [process.close_stderr_calls for process in processes] == [1, 1]


def test_failed_later_forward_batch_preserves_committed_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed_service = ForwardSpec("gdb", 32232, 3333)
    failed_forwards = (ForwardSpec("tcl", 32243, 6333), ForwardSpec("telnet", 32254, 4444))
    committed_process = _ForwardProcess(None)
    failed_processes = (_ForwardProcess(None), _ForwardProcess(None))
    manager = ForwardManager(
        _ForwardCommand(committed_process, *failed_processes),
        "host",
    )
    readiness = iter((True, True, False))
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    monkeypatch.setattr(
        forwarding_module,
        "await_ready",
        lambda *_args: next(readiness),
    )

    manager.start((committed_service,), "127.64.0.1")
    with pytest.raises(SessionError):
        manager.start(failed_forwards, "127.64.0.1")

    assert committed_process.terminate_calls == 0
    assert [process.terminate_calls for process in failed_processes] == [1, 1]
    assert committed_process.close_stderr_calls == 0
    assert [process.close_stderr_calls for process in failed_processes] == [1, 1]
    assert manager.has_forwards

    manager.close()
    assert committed_process.terminate_calls == 1
    assert [process.terminate_calls for process in failed_processes] == [1, 1]
    assert committed_process.close_stderr_calls == 1
    assert [process.close_stderr_calls for process in failed_processes] == [1, 1]


@pytest.mark.parametrize("phase", ("launch-handoff", "readiness"))
def test_interruption_during_forward_start_preserves_cleanup_ownership(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    forward = ForwardSpec("gdb", 32265, 3333)
    initialize = subprocess.Popen.__init__
    read = os.read
    managed_processes: list[ManagedSshProcess] = []
    interruption = KeyboardInterrupt("forward startup interrupted")

    class InterruptingCommand(SshCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired: Callable[[ManagedSshProcess], None] | None = None,
        ) -> ManagedSshProcess:
            process = super().popen(
                host, remote_command, local_forward=local_forward, on_acquired=on_acquired
            )
            managed_processes.append(process)
            if phase == "launch-handoff":
                raise interruption
            return process

    manager = ForwardManager(InterruptingCommand(), "host")

    # Popen's keyword arguments pass through unchanged.
    def launch(process: subprocess.Popen[bytes], argv: list[str], **kwargs: Any) -> None:
        # Substitute the executable only; production transport management,
        # readiness observation, and cleanup still operate on a real child.
        code = shlex.split(argv[-1])[2]
        initialize(process, [sys.executable, "-u", "-c", code], **kwargs)

    def read_readiness(descriptor: int, size: int) -> bytes:
        if phase == "readiness" and any(
            process.stdout is not None and descriptor == process.stdout.fileno()
            for process in managed_processes
        ):
            raise interruption
        return read(descriptor, size)

    monkeypatch.setattr(subprocess.Popen, "__init__", launch)
    monkeypatch.setattr(os, "read", read_readiness)
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            manager.start((forward,), "127.64.0.1")
        assert raised.value is interruption
        assert manager.forwards == ()
        # Rollback ran inside start(); every acquired transport is settled.
        assert managed_processes
        for process in managed_processes:
            assert process.poll() is not None
            assert process.stdin is not None and process.stdin.closed
            assert process.stdout is not None and process.stdout.closed
    finally:
        manager.close()
        for process in managed_processes:
            stop_process(process)


@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_ctrl_c_between_pending_forward_rollbacks_skips_no_cleanup(
    monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    initialize = subprocess.Popen.__init__
    previous_trace = sys.gettrace()
    processes: list[ManagedSshProcess] = []
    stderr_streams: list[IO[bytes]] = []
    startup_error = SessionError("required forward readiness failed")
    cleanup_error = OSError("first forward stream cleanup failed")
    requested = False

    class Command(SshCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired: Callable[[ManagedSshProcess], None] | None = None,
        ) -> ManagedSshProcess:
            process = super().popen(
                host, remote_command, local_forward=local_forward, on_acquired=on_acquired
            )
            processes.append(process)
            if cleanup_fails and len(processes) == 1:
                close_stderr = process.close_stderr

                def fail_close_stderr() -> None:
                    close_stderr()
                    raise cleanup_error

                monkeypatch.setattr(process, "close_stderr", fail_close_stderr)
            return process

    manager = ForwardManager(Command(), "target")
    await_ready = forwarding_module.await_ready

    # Popen's keyword arguments pass through unchanged.
    def launch(process: subprocess.Popen[bytes], argv: list[str], **kwargs: Any) -> None:
        code = shlex.split(argv[-1])[2]
        initialize(process, [sys.executable, "-u", "-c", code], **kwargs)
        assert process.stderr is not None
        stderr_streams.append(process.stderr)

    def observe_readiness(process: ManagedSshProcess, sentinel: str, deadline: float) -> bool:
        assert await_ready(process, sentinel, deadline)
        if len(processes) == 2:
            raise startup_error
        return True

    def all_closed() -> bool:
        return all(
            process.returncode is not None
            and process.stdin is not None
            and process.stdin.closed
            and process.stdout is not None
            and process.stdout.closed
            for process in processes
        ) and all(stream.closed for stream in stderr_streams)

    def interrupt_between_rollbacks(
        frame: FrameType, event: str, _arg: object
    ) -> TraceFunction | None:
        nonlocal requested
        if (
            event == "line"
            and frame.f_code is ForwardManager.start.__code__
            and not requested
            and len(processes) == 2
        ):
            first = processes[0]
            second = processes[1]
            if (
                first.returncode is not None
                and first.stdin is not None
                and first.stdin.closed
                and first.stdout is not None
                and first.stdout.closed
                and stderr_streams[0].closed
                and second.returncode is None
            ):
                requested = True
                # Repeated runner Ctrl-C during rollback only latches.
                signal.raise_signal(signal.SIGINT)
                signal.raise_signal(signal.SIGINT)
        return interrupt_between_rollbacks

    monkeypatch.setattr(subprocess.Popen, "__init__", launch)
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    monkeypatch.setattr(forwarding_module, "await_ready", observe_readiness)
    failure = None
    try:
        with (
            pytest.raises(forwarding_module.ForwardStartError) as raised,
            operation_scope() as scope,
        ):
            sys.settrace(interrupt_between_rollbacks)
            try:
                manager.start(
                    (ForwardSpec("gdb", 32266, 3333), ForwardSpec("rtt", 32267, 5555)),
                    "127.64.0.1",
                )
            finally:
                sys.settrace(previous_trace)
        failure = raised.value

        assert requested and len(processes) == 2
        # Check actual reaping and every pipe before any test fallback cleanup.
        assert all_closed()
        assert scope.requested
        # The established startup failure stays primary; cancellation is noted.
        assert failure.cause is startup_error
        assert any("cancellation" in note for note in failure.__notes__)
        if cleanup_fails:
            assert cleanup_error in failure.cleanup_errors
            assert any(str(cleanup_error) in note for note in failure.__notes__)
        assert not manager.has_forwards
    finally:
        sys.settrace(previous_trace)
        manager.close()
        for process in processes:
            with suppress(OSError):
                stop_process(process)


@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_interruption_at_forward_rollback_entry_keeps_both_transports_owned(
    monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool
) -> None:
    initialize = subprocess.Popen.__init__
    await_ready = forwarding_module.await_ready
    previous_trace = sys.gettrace()
    processes: list[ManagedSshProcess] = []
    stderr_streams: list[IO[bytes]] = []
    startup_error = SessionError("second forward readiness failed")
    cleanup_error = OSError("first pending forward stream cleanup failed")
    interruption = KeyboardInterrupt("rollback entry interrupted")
    requested = False
    both_live_at_delivery = False

    class Command(SshCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired: Callable[[ManagedSshProcess], None] | None = None,
        ) -> ManagedSshProcess:
            process = super().popen(
                host, remote_command, local_forward=local_forward, on_acquired=on_acquired
            )
            processes.append(process)
            if cleanup_fails and len(processes) == 1:
                close_stderr = process.close_stderr

                def fail_close_stderr() -> None:
                    close_stderr()
                    raise cleanup_error

                monkeypatch.setattr(process, "close_stderr", fail_close_stderr)
            return process

    session = SessionController(
        SessionPlan("target", Command(), ProcessSpec(("unused",))),
        DeploymentResult("/helper.py", "digest", False),
    )
    manager = session._forwards

    # Popen's keyword arguments pass through unchanged.
    def launch(process: subprocess.Popen[bytes], argv: list[str], **kwargs: Any) -> None:
        code = shlex.split(argv[-1])[2]
        initialize(process, [sys.executable, "-u", "-c", code], **kwargs)
        assert process.stderr is not None
        stderr_streams.append(process.stderr)

    def observe_readiness(process: ManagedSshProcess, sentinel: str, deadline: float) -> bool:
        assert await_ready(process, sentinel, deadline)
        if len(processes) == 2:
            raise startup_error
        return True

    def interrupt_at_rollback_entry(
        frame: FrameType, event: str, _arg: object
    ) -> TraceFunction | None:
        nonlocal requested, both_live_at_delivery
        # The exception is bound, but the handler's first instruction has not
        # run. No line number or scheduler timing selects this entry boundary.
        if (
            event == "line"
            and frame.f_code is ForwardManager.start.__code__
            and frame.f_locals.get("error") is startup_error
            and not requested
        ):
            requested = True
            both_live_at_delivery = len(processes) == 2 and all(
                process.poll() is None for process in processes
            )
            raise interruption
        return interrupt_at_rollback_entry

    monkeypatch.setattr(subprocess.Popen, "__init__", launch)
    monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)
    monkeypatch.setattr(forwarding_module, "await_ready", observe_readiness)
    try:
        sys.settrace(interrupt_at_rollback_entry)
        try:
            with pytest.raises(KeyboardInterrupt) as raised:
                manager.start(
                    (ForwardSpec("gdb", 32268, 3333), ForwardSpec("rtt", 32269, 5555)), "127.64.0.1"
                )
        finally:
            sys.settrace(previous_trace)
        assert raised.value is interruption
        assert requested and both_live_at_delivery
        assert manager.forwards == ()

        restart_failure = None
        try:
            manager.start((), "127.64.0.1")
        except SessionError as error:
            restart_failure = error

        close_failure = None
        try:
            session.close()
        except OSError as error:
            close_failure = error

        # Only production session/manager cleanup has run at this point.
        assert all(process.returncode is not None for process in processes)
        assert all(process.stdin is not None and process.stdin.closed for process in processes)
        assert all(process.stdout is not None and process.stdout.closed for process in processes)
        assert all(stream.closed for stream in stderr_streams)
        assert restart_failure is not None
        assert close_failure is (cleanup_error if cleanup_fails else None)
        assert session.closed
        assert not manager.forwards
        session.close()
    finally:
        sys.settrace(previous_trace)
        with suppress(BaseException):
            session.close()
        for process in processes:
            with suppress(OSError):
                stop_process(process)


class _ForwardProcess:
    def __init__(
        self,
        returncode: int | None,
        *,
        terminate_error: BaseException | None = None,
        close_stderr_error: BaseException | None = None,
    ) -> None:
        self.stdin = io.BytesIO()
        self.stdout = None
        self.stderr = None
        self.returncode = returncode
        self.args = ("fake-forward",)
        self.terminate_error = terminate_error
        self.close_stderr_error = close_stderr_error
        self.terminate_calls = 0
        self.close_stderr_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -signal.SIGKILL

    def stderr_tail(self) -> bytes:
        return b""

    def close_stderr(self) -> None:
        self.close_stderr_calls += 1
        if self.close_stderr_error is not None:
            raise self.close_stderr_error


class _PreflightSocket:
    def __init__(self, occupied: Collection[int]) -> None:
        self.occupied = occupied

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> Literal[False]:
        return False

    def bind(self, address: tuple[str, int]) -> None:
        assert address[0] == "127.0.0.1"
        if address[1] in self.occupied:
            raise OSError("address already in use")


def _patch_preflight_socket(monkeypatch: pytest.MonkeyPatch, occupied: Collection[int]) -> None:
    monkeypatch.setattr(
        forwarding_module,
        "socket",
        SimpleNamespace(socket=lambda: _PreflightSocket(occupied)),
    )
