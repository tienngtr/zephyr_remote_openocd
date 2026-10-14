# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import fcntl
import io
import json
import os
import signal
import subprocess
import sys
from collections.abc import Buffer, Callable
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import IO, cast
from unittest.mock import create_autospec

import pytest
from zephyr_remote_openocd.helper import control as helper_control
from zephyr_remote_openocd.helper import process as helper_process
from zephyr_remote_openocd.helper import session as helper_session
from zephyr_remote_openocd.helper import streams as helper_streams
from zephyr_remote_openocd.helper import workspace as workspace_module
from zephyr_remote_openocd.helper.lifecycle import Closed
from zephyr_remote_openocd.protocol import commands as protocol_commands
from zephyr_remote_openocd.protocol.commands import write_start
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy, Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec, RequiredPathSpec
from zephyr_remote_openocd.protocol.wire import decode_terminal

from tests.helper_process_support import cleanup_supervised_process

SAMPLE_CHILD_EXIT_CODE = 7
SignalHandler = Callable[[int, FrameType | None], object] | int | None


@pytest.fixture
def control_pipe(monkeypatch):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb", buffering=0) as writer:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=reader))
        yield reader, writer


@pytest.mark.parametrize("boundary", ("constructor", "loop"))
def test_coordinator_adoption_failure_attempts_lock_cleanup_after_workspace_failure(
    tmp_path, monkeypatch, boundary
):
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    allocate = workspace_module.new_workspace
    remove = workspace_module.remove_workspace
    acquired = []
    failure = RuntimeError("coordinator construction failed")
    cleanup_failure = OSError("workspace cleanup failed")
    cleanup_detail = "workspace lease cleanup also failed"
    cleanup_failure.add_note(cleanup_detail)

    def new_workspace():
        resources = allocate()
        acquired.append(resources)
        return resources

    def fail_remove(_workspace):
        raise cleanup_failure

    monkeypatch.setattr(workspace_module, 'new_workspace', new_workspace)
    monkeypatch.setattr(workspace_module, 'remove_workspace', fail_remove)
    if boundary == "constructor":
        monkeypatch.setattr(asyncio, "Queue", create_autospec(asyncio.Queue, side_effect=failure))
    else:
        monkeypatch.setattr(
            asyncio.events,
            "new_event_loop",
            create_autospec(asyncio.events.new_event_loop, side_effect=failure),
        )
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with pytest.raises(SystemExit) as raised:
            helper_control.control()
        assert (raised.value.__cause__ or raised.value) is failure
        assert acquired[0][2].closed
        assert any(str(cleanup_failure) in note for note in failure.__notes__)
        assert any(cleanup_detail in note for note in failure.__notes__)
        assert all(
            signal.getsignal(signum) == handler for signum, handler in previous_handlers.items()
        )
    finally:
        if acquired:
            acquired[0][2].close()
            remove(acquired[0][1])


def _start_session_command(argv, sentinels=()):
    return (
        json.dumps(
            {
                "version": 1,
                "type": "START",
                "completion_policy": "live_server",
                "argv": argv,
                "environment": {},
                "required_paths": [],
                "services": [],
                "required_output_sentinels": list(sentinels),
                "readiness_timeout": 30,
                "literal_prefix": len(argv),
                "argv_templates": [],
                "preferred_address": None,
            }
        ).encode()
        + b"\n"
    )


@pytest.fixture
def coordinated_helper(tmp_path, monkeypatch, control_pipe):
    """Use real workspace, child owners, pipes, and the lifecycle coordinator."""
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    session = helper_session.HelperSession.create()
    _, writer = control_pipe
    events = []
    children = []
    spawn = helper_process._spawn_child

    def acquire(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(helper_process, '_spawn_child', acquire)
    monkeypatch.setattr(
        helper_streams, 'emit', lambda kind, **fields: events.append((kind, fields))
    )
    yield session, writer, events, children
    for child in children:
        cleanup_supervised_process(child, None)
    session._release_workspace()


def _terminal_event(events):
    terminals = [fields for kind, fields in events if kind == "SESSION_ENDED"]
    assert len(terminals) == 1
    return decode_terminal(
        json.loads(json.dumps(dict(version=1, type="SESSION_ENDED", **terminals[0])))
    )


def _run_terminated(session, *, fails):
    if fails:
        with pytest.raises(SystemExit) as raised:
            session.run()
        assert raised.value.code == 1
    else:
        session.run()
    assert isinstance(session.lifecycle.state, Closed)


@pytest.mark.parametrize("suffix", (b"\n", b" ", b'{"version":1,"type":"UNEXPECTED"}\n', b"junk"))
def test_post_start_bytes_are_failure_even_in_same_read(coordinated_helper, suffix):
    session, writer, events, _children = coordinated_helper
    stream = io.BytesIO()
    write_start(stream, ProcessSpec((sys.executable, "-c", "import signal;signal.pause()")), ())
    writer.write(stream.getvalue() + suffix)
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.trigger == Trigger.PROTOCOL_FAILURE
    assert snapshot.outcome.primary_failure is not None
    assert snapshot.cleanup.confirmed
    assert not _children  # Reject the batched invalid input before physical entry.


@pytest.mark.parametrize("partial", (False, True))
def test_controller_eof_before_start_has_explicit_result(coordinated_helper, partial):
    session, writer, events, _children = coordinated_helper
    if partial:
        writer.write(b'{"version":1')
    writer.close()
    _run_terminated(session, fails=partial)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.trigger == (
        Trigger.PROTOCOL_FAILURE if partial else Trigger.CONTROLLER_EOF
    )
    assert snapshot.outcome.child_result is None
    assert snapshot.cleanup.confirmed


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_missing_required_path_fails_before_attempt_admission(coordinated_helper, kind):
    session, writer, events, children = coordinated_helper
    missing = session.work / "staged" / "missing"
    write_start(
        writer,
        ProcessSpec((sys.executable,), required_paths=(RequiredPathSpec(str(missing), kind),)),
        (),
    )
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.primary_failure is not None
    assert str(missing) in snapshot.outcome.primary_failure.message
    assert not children and not any(event == "ATTEMPT" for event, _ in events)
    assert snapshot.cleanup.confirmed


@pytest.mark.parametrize("policy", tuple(CompletionPolicy))
def test_completion_policy_controls_readiness(coordinated_helper, monkeypatch, policy):
    session, writer, events, children = coordinated_helper
    record = helper_streams.emit

    def emit(kind, **fields):
        record(kind, **fields)
        if kind == "READY":
            writer.close()

    monkeypatch.setattr(helper_streams, 'emit', emit)
    code = (
        "print('done',flush=True)"
        if policy == CompletionPolicy.PROCESS_EXIT
        else "import signal;print('ready',flush=True);signal.pause()"
    )
    write_start(writer, ProcessSpec((sys.executable, "-c", code), completion_policy=policy), ())
    _run_terminated(session, fails=False)
    kinds = [kind for kind, _ in events]
    assert ("READY" in kinds) == (policy == CompletionPolicy.LIVE_SERVER)
    assert kinds.index("ATTEMPT") < kinds.index("SESSION_ENDED")
    snapshot = _terminal_event(events)
    assert snapshot.cleanup.confirmed
    assert snapshot.outcome.child_result is not None
    assert snapshot.outcome.child_result.termination_requested == (
        policy == CompletionPolicy.LIVE_SERVER
    )
    assert all(child.process.stdout.closed and child.process.stderr.closed for child in children)
    assert not session.work.exists() and session.workspace_lock.closed


@pytest.mark.parametrize("admission", ("ATTEMPT", "READY", "SESSION_ENDED"))
def test_admission_failure_does_not_publish_success_or_repeat_terminal(
    coordinated_helper, monkeypatch, admission
):
    session, writer, events, children = coordinated_helper
    record = helper_streams.emit
    attempts = []

    def emit(kind, **fields):
        attempts.append(kind)
        if kind == admission:
            if kind == "SESSION_ENDED":
                assert not session.work.exists()
            raise BrokenPipeError("admission failed")
        record(kind, **fields)
        if kind == "READY":
            writer.close()

    monkeypatch.setattr(helper_streams, 'emit', emit)
    write_start(writer, ProcessSpec((sys.executable, "-c", "import signal;signal.pause()")), ())
    _run_terminated(session, fails=True)
    assert attempts.count("SESSION_ENDED") == 1
    assert len(children) == (0 if admission == "ATTEMPT" else 1)
    assert session.lifecycle.state.snapshot.cleanup.confirmed
    assert not session.work.exists()
    if admission == "SESSION_ENDED":
        state = session.lifecycle.state
        assert isinstance(state, Closed)
        assert state.local_diagnostics
    else:
        assert _terminal_event(events).outcome.primary_failure.code == admission + "_ADMISSION"


@pytest.mark.parametrize("boundary", ("before_spawn", "returned", "wrapper_failure"))
def test_signal_capture_and_spawn_handoff_keep_cleanup_reachable(
    coordinated_helper, monkeypatch, boundary
):
    session, writer, events, _children = coordinated_helper
    spawn = helper_process._spawn_child
    supervisor = helper_process.SupervisedProcess

    def acquire(*args, **kwargs):
        if boundary == "before_spawn":
            session.handle_signal(signal.SIGTERM)
        child = spawn(*args, **kwargs)
        if boundary == "returned":
            session.handle_signal(signal.SIGINT)
            raise KeyboardInterrupt("interrupted after return")
        return child

    def construct(*args, **kwargs):
        session.handle_signal(signal.SIGTERM)
        raise RuntimeError("supervisor adoption failed")

    monkeypatch.setattr(helper_process, '_spawn_child', acquire)
    if boundary == "wrapper_failure":
        monkeypatch.setattr(helper_process, 'SupervisedProcess', construct)
    write_start(writer, ProcessSpec((sys.executable, "-c", "import signal;signal.pause()")), ())
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.primary_failure is not None
    assert snapshot.cleanup.confirmed
    assert "READY" not in [kind for kind, _ in events]
    assert not session.work.exists() and session.workspace_lock.closed
    monkeypatch.setattr(helper_process, 'SupervisedProcess', supervisor)


@pytest.mark.parametrize("failure_type", (MemoryError, SystemExit))
def test_raw_child_return_failure_keeps_disposal_owned(coordinated_helper, failure_type):
    session, writer, events, _children = coordinated_helper
    processes: list[subprocess.Popen[bytes]] = []
    failure = failure_type("raw child registration interrupted")
    previous_trace = sys.gettrace()

    def fail_at_return(frame: FrameType, event: str, _arg: object):
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            child = cast(subprocess.Popen[bytes], frame.f_locals["self"])
            assert child.pid is not None
            processes.append(child)
            raise failure
        return fail_at_return

    write_start(writer, ProcessSpec((sys.executable, "-c", "import signal; signal.pause()")), ())
    try:
        sys.settrace(fail_at_return)
        try:
            _run_terminated(session, fails=True)
        finally:
            sys.settrace(previous_trace)

        assert len(processes) == 1
        child = processes[0]
        # Inspect before fallback cleanup: session rollback must settle the raw
        # process even when normal constructor-return adoption did not occur.
        assert child.returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        assert child.stdout is not None and child.stdout.closed
        assert child.stderr is not None and child.stderr.closed
        snapshot = _terminal_event(events)
        assert snapshot.outcome.primary_failure is not None
        assert snapshot.cleanup.child_disposal == "confirmed"
        assert snapshot.cleanup.confirmed
        assert not session.work.exists() and session.workspace_lock.closed
    finally:
        sys.settrace(previous_trace)
        for child in processes:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()


@pytest.mark.parametrize(
    "failed_tasks",
    (
        ("process-group cleanup",),
        ("stdout",),
        ("stderr",),
        ("stdout", "stderr", "process-group cleanup"),
        ("process-group cleanup", "output drain deadline"),
    ),
    ids=("group-task", "stdout-task", "stderr-task", "group-and-relays", "group-and-deadline"),
)
@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt))
def test_cleanup_task_setup_failure_still_disposes_child_and_independent_resources(
    coordinated_helper, monkeypatch, failed_tasks, failure_type
):
    session, writer, events, children = coordinated_helper
    create_task = asyncio.TaskGroup.create_task
    record = helper_streams.emit
    failures = []

    def create(group, coroutine, *, name=None, **kwargs):
        if name in failed_tasks:
            failure = failure_type(f"cannot create {name}")
            failure.add_note("task acquisition diagnostic")
            failures.append(failure)
            raise failure
        return create_task(group, coroutine, name=name, **kwargs)

    def emit(kind, **fields):
        record(kind, **fields)
        if kind == "READY":
            writer.close()

    monkeypatch.setattr(asyncio.TaskGroup, "create_task", create)
    monkeypatch.setattr(helper_streams, 'emit', emit)
    write_start(writer, ProcessSpec((sys.executable, "-c", "import signal;signal.pause()")), ())
    _run_terminated(session, fails=True)

    snapshot = _terminal_event(events)
    assert snapshot.cleanup.confirmed
    assert snapshot.outcome.primary_failure is not None
    assert snapshot.outcome.primary_failure.message == str(failures[0])
    assert all(str(failure) in repr(snapshot.outcome) for failure in failures)
    assert "task acquisition diagnostic" in repr(snapshot.outcome)
    assert len(children) == 1
    child = children[0]
    assert child.process.returncode is not None and child.group_disposed
    assert child.process.stdout.closed and child.process.stderr.closed
    assert all(task.done() for task in child.tasks)
    assert session.workspace_lock.closed and not session.work.exists()
    assert not workspace_module._lease_path(session.work).exists()
    assert not workspace_module._closure_path(session.work).exists()


@pytest.mark.parametrize("failure", ("stdout", "stderr", "workspace"))
def test_nested_failure_detail_survives_cleanup_and_wire(coordinated_helper, monkeypatch, failure):
    session, writer, events, children = coordinated_helper
    nested = RuntimeError("nested cause")
    nested.add_note("nested retained note")
    grouped = ExceptionGroup(
        "composed failure", [OSError("first cause"), ExceptionGroup("inner", [nested])]
    )
    grouped.add_note("outer retained note")
    restoration = OSError("output descriptor restoration failed")
    restoration.add_note("nested restoration diagnostic")
    if failure != "workspace":
        read = os.read
        set_blocking = os.set_blocking

        def is_output(descriptor):
            return bool(children) and descriptor == getattr(children[0].process, failure).fileno()

        def fail_read(descriptor, size):
            if is_output(descriptor):
                raise grouped
            return read(descriptor, size)

        def fail_restore(descriptor, blocking):
            set_blocking(descriptor, blocking)
            if blocking and is_output(descriptor):
                raise restoration

        monkeypatch.setattr(os, "read", fail_read)
        monkeypatch.setattr(os, "set_blocking", fail_restore)
    else:

        def fail_removal(_work):
            raise grouped

        monkeypatch.setattr(workspace_module, 'remove_workspace', fail_removal)
        record = helper_streams.emit

        def emit(kind, **fields):
            record(kind, **fields)
            if kind == "READY":
                writer.close()

        monkeypatch.setattr(helper_streams, 'emit', emit)
    write_start(writer, ProcessSpec((sys.executable, "-c", "import signal;signal.pause()")), ())
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    detail = snapshot.outcome.primary_failure
    assert detail is not None
    assert detail.message == str(grouped)
    assert detail.diagnostics[1].diagnostics[0].diagnostics[0].message == "nested retained note"
    assert "outer retained note" in [diagnostic.message for diagnostic in detail.diagnostics]
    if failure != "workspace":
        assert str(restoration) in repr(detail)
        assert "nested restoration diagnostic" in repr(detail)
    assert children[0].process.returncode is not None
    assert children[0].group_disposed
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert session.workspace_lock.closed
    assert snapshot.cleanup.confirmed == (failure != "workspace")


def test_control_protocol_failure_survives_descriptor_restoration_failure(
    coordinated_helper, control_pipe, monkeypatch
):
    session, writer, events, children = coordinated_helper
    reader, _writer = control_pipe
    descriptor = reader.fileno()
    restoration = OSError("control descriptor restoration failed")
    restoration.add_note("nested restoration diagnostic")
    set_blocking = os.set_blocking

    def fail_restore(fd, blocking):
        set_blocking(fd, blocking)
        if fd == descriptor and blocking:
            raise restoration

    monkeypatch.setattr(os, "set_blocking", fail_restore)
    command = {"version": 1, "type": "UNEXPECTED"}
    with pytest.raises(ValueError) as invalid:
        protocol_commands.decode_command(command)
    writer.write(json.dumps(command).encode() + b"\n")
    _run_terminated(session, fails=True)

    snapshot = _terminal_event(events)
    detail = snapshot.outcome.primary_failure
    assert detail is not None and detail.message == str(invalid.value)
    assert snapshot.outcome.trigger == Trigger.PROTOCOL_FAILURE
    assert str(restoration) in repr(detail)
    assert "nested restoration diagnostic" in repr(detail)
    assert snapshot.cleanup.confirmed and not children
    assert session.workspace_lock.closed and not session.work.exists()


@pytest.mark.parametrize("settlement", ("eof", "cancellation"))
def test_observer_settlement_retains_descriptor_restoration_failure(
    coordinated_helper, control_pipe, monkeypatch, settlement
):
    session, writer, events, children = coordinated_helper
    reader, _writer = control_pipe
    descriptor = reader.fileno()
    restoration = OSError("control descriptor restoration failed")
    restoration.add_note("nested restoration diagnostic")
    set_blocking = os.set_blocking

    def fail_restore(fd, blocking):
        set_blocking(fd, blocking)
        if fd == descriptor and blocking:
            raise restoration

    monkeypatch.setattr(os, "set_blocking", fail_restore)
    if settlement == "eof":
        writer.close()
    else:
        add_reader = asyncio.SelectorEventLoop.add_reader

        def cancel_on_registered_input(loop, fd, callback, *args):
            add_reader(loop, fd, callback, *args)
            if fd == descriptor:
                # The control observer is suspended on its registered reader
                # before signal-driven coordinator cleanup cancels it.
                session.handle_signal(signal.SIGTERM)

        monkeypatch.setattr(asyncio.SelectorEventLoop, "add_reader", cancel_on_registered_input)
    _run_terminated(session, fails=True)

    snapshot = _terminal_event(events)
    assert str(restoration) in repr(snapshot.outcome)
    assert "nested restoration diagnostic" in repr(snapshot.outcome)
    if settlement == "cancellation":
        assert snapshot.outcome.trigger == Trigger.SIGNAL
    assert snapshot.cleanup.confirmed and not children
    assert session.workspace_lock.closed and not session.work.exists()


@pytest.mark.parametrize("final_observation", ("ready", "exit", "timeout"))
def test_readiness_deadline_processes_final_visible_observation(
    tmp_path, monkeypatch, control_pipe, final_observation
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / workspace_module.SESSION_LOCK).open("w+b")
    session = helper_session.HelperSession("session", workspace, lock)
    children = []
    events = []
    original_spawn = helper_process._spawn_child
    original_read = os.read
    original_sleep = asyncio.sleep
    output_blocked = True

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    def controlled_read(descriptor, size):
        stream = children[0].process.stdout if children else None
        if output_blocked and stream is not None and descriptor == stream.fileno():
            raise BlockingIOError
        return original_read(descriptor, size)

    async def controlled_sleep(delay):
        nonlocal output_blocked
        if delay == 30:
            # Observe readiness through a duplicate fd without reading any bytes.
            stream = children[0].process.stdout
            assert stream is not None
            descriptor = os.dup(stream.fileno())
            try:
                await helper_streams._readable(descriptor)
            finally:
                os.close(descriptor)
            if final_observation == "exit":
                child = children[0]
                descriptor = os.pidfd_open(child.pid)
                try:
                    os.kill(child.pid, signal.SIGUSR1)
                    await helper_streams._readable(descriptor)
                finally:
                    os.close(descriptor)
            # The deadline producer enqueues its fact before yielding again.
            output_blocked = False
        else:
            await original_sleep(delay)

    def emit(kind, **values):
        events.append((kind, values))
        if kind == "READY":
            writer.close()

    monkeypatch.setattr(helper_process, '_spawn_child', spawn)
    monkeypatch.setattr(os, "read", controlled_read)
    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(helper_streams, 'emit', emit)
    marker = "diagnostic" if final_observation == "timeout" else "READY"
    writer.write(
        _start_session_command(
            [
                sys.executable,
                "-c",
                "import signal,sys;"
                "signal.signal(signal.SIGUSR1,lambda *_:sys.exit(7));"
                f"print({marker!r},flush=True);signal.pause()",
            ],
            ("READY",),
        )
    )

    _run_terminated(session, fails=final_observation != "ready")
    snapshot = _terminal_event(events)
    assert [kind for kind, _ in events].count("READY") == (1 if final_observation == "ready" else 0)
    assert snapshot.cleanup.confirmed
    if final_observation == "exit":
        assert snapshot.outcome.child_result.returncode == SAMPLE_CHILD_EXIT_CODE
    elif final_observation == "timeout":
        assert snapshot.outcome.primary_failure.code == "READINESS_TIMEOUT"
    assert children[0].process.returncode is not None
    assert not workspace.exists()
    assert lock.closed


@pytest.mark.parametrize("failure_type", (OSError, asyncio.CancelledError))
@pytest.mark.parametrize("restoration_fails", (False, True))
def test_partial_signal_installation_restores_handlers_and_preserves_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    restoration_fails: bool,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = helper_session.HelperSession.create()
    failure = failure_type("injected signal installation failure")
    restoration_failure = OSError("injected signal restoration failure")
    restoration_detail = "signal restoration retained detail"
    restoration_failure.add_note(restoration_detail)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    original_signal = signal.signal
    installed: set[int] = set()
    restored: list[int] = []

    def install_then_fail(signum: int, handler: SignalHandler) -> SignalHandler:
        result = original_signal(signum, handler)
        if handler == session.handle_signal:  # pylint: disable=comparison-with-callable
            installed.add(signum)
            if signum == signal.SIGINT:
                raise failure
        elif signum in installed:
            restored.append(signum)
            installed.remove(signum)
            if restoration_fails:
                raise restoration_failure
        return result

    monkeypatch.setattr(signal, "signal", install_then_fail)

    async def run() -> None:
        tasks_before = asyncio.all_tasks()
        output_before = helper_streams._protocol_output.get()
        with pytest.raises(SystemExit):
            await session.run_async()
        state = session.lifecycle.state
        assert isinstance(state, Closed)
        assert state.snapshot.outcome.primary_failure is not None
        assert state.snapshot.outcome.primary_failure.message == str(failure)
        assert asyncio.all_tasks() == tasks_before
        assert helper_streams._protocol_output.get() is output_before

    try:
        asyncio.run(run())
        assert set(restored) == set(previous)
        assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())
        if restoration_fails:
            state = session.lifecycle.state
            assert isinstance(state, Closed)
            assert restoration_detail in repr(state.local_diagnostics)
        assert session.workspace_lock.closed
        assert not session.work.exists()
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        session._release_workspace()


@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_signal_restoration_attempts_both_handlers_after_normal_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_pipe: tuple[IO[bytes], IO[bytes]],
    failure_type: type[BaseException],
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = helper_session.HelperSession.create()
    _reader, writer = control_pipe
    writer.close()
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    original_signal = signal.signal
    failures: dict[int, BaseException] = {
        signum: failure_type(f"restoration failed for {signum}") for signum in previous
    }
    installed: set[int] = set()
    restored: list[int] = []

    def restore_then_fail(signum: int, handler: SignalHandler) -> SignalHandler:
        result = original_signal(signum, handler)
        if handler == session.handle_signal:  # pylint: disable=comparison-with-callable
            installed.add(signum)
        elif signum in installed:
            restored.append(signum)
            installed.remove(signum)
            raise failures[signum]
        return result

    monkeypatch.setattr(signal, "signal", restore_then_fail)
    try:
        with pytest.raises(SystemExit):
            session.run()
        assert set(restored) == set(previous)
        state = session.lifecycle.state
        assert isinstance(state, Closed)
        assert len(state.local_diagnostics) == 2
        assert state.snapshot.outcome.primary_failure is None
        assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())
        assert session.workspace_lock.closed
        assert not session.work.exists()
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        session._release_workspace()


@pytest.mark.parametrize("primary_type", (None, OSError, asyncio.CancelledError))
@pytest.mark.parametrize("close_type", (OSError, KeyboardInterrupt))
def test_protocol_writer_and_close_failures_preserve_primary_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_pipe: tuple[IO[bytes], IO[bytes]],
    primary_type: type[BaseException] | None,
    close_type: type[BaseException],
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = helper_session.HelperSession.create()
    _control_reader, control_writer = control_pipe
    control_writer.close()
    primary = primary_type("injected session setup failure") if primary_type is not None else None
    writer_failure = BrokenPipeError("injected protocol write failure")
    close_failure = close_type("injected protocol descriptor restoration failure")
    close_detail = "protocol descriptor cleanup retained detail"
    close_failure.add_note(close_detail)
    original_signal = signal.signal
    original_write = os.write
    original_set_blocking = os.set_blocking
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    output_read, output_write = os.pipe()
    close_attempts: list[int] = []

    def install_then_fail(signum: int, handler: SignalHandler) -> SignalHandler:
        result = original_signal(signum, handler)
        if (
            signum == signal.SIGINT
            and handler == session.handle_signal  # pylint: disable=comparison-with-callable
            and primary is not None
        ):
            raise primary
        return result

    def fail_write(descriptor: int, payload: Buffer) -> int:
        if descriptor == output_write:
            raise writer_failure
        return original_write(descriptor, payload)

    def restore_then_fail(descriptor: int, blocking: bool) -> None:
        original_set_blocking(descriptor, blocking)
        if descriptor == output_write and blocking:
            close_attempts.append(descriptor)
            raise close_failure

    async def run() -> None:
        tasks_before = asyncio.all_tasks()
        output_before = helper_streams._protocol_output.get()
        with pytest.raises(SystemExit):
            await session.run_async()
        state = session.lifecycle.state
        assert isinstance(state, Closed)
        assert close_detail in repr(state.local_diagnostics)
        all_detail = repr(state.snapshot.outcome) + repr(state.local_diagnostics)
        assert str(writer_failure) in all_detail
        if primary is not None:
            assert state.snapshot.outcome.primary_failure is not None
            assert state.snapshot.outcome.primary_failure.message == str(primary)
        assert asyncio.all_tasks() == tasks_before
        assert helper_streams._protocol_output.get() is output_before

    try:
        with (
            os.fdopen(output_read, "rb"),
            os.fdopen(output_write, "w", encoding="utf-8") as stdout,
            monkeypatch.context() as patch,
        ):
            patch.setattr(sys, "stdout", stdout)
            patch.setattr(signal, "signal", install_then_fail)
            patch.setattr(os, "write", fail_write)
            patch.setattr(os, "set_blocking", restore_then_fail)
            asyncio.run(run())
            assert os.get_blocking(output_write)
        assert close_attempts == [output_write]
        assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())
        assert session.workspace_lock.closed
        assert not session.work.exists()
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        session._release_workspace()


@pytest.mark.parametrize("cleanup_failure", ("group", "lease"))
def test_unconfirmed_disposal_retains_inputs_but_settles_independent_owners(
    coordinated_helper, monkeypatch, cleanup_failure
):
    session, writer, events, children = coordinated_helper
    record = helper_streams.emit
    terminate = helper_process.SupervisedProcess.terminate
    lease = None

    async def uncertain(child):
        await terminate(child)
        child.group_disposed = False
        raise OSError("group disposal could not be established")

    class UnconfirmedLease:
        def close(self):
            raise OSError("address lease close failed")

    def emit(kind, **fields):
        nonlocal lease
        record(kind, **fields)
        if kind == "READY":
            if cleanup_failure == "lease":
                lease = session._address_lease
                monkeypatch.setattr(session, "_address_lease", UnconfirmedLease())
            writer.close()

    monkeypatch.setattr(helper_streams, 'emit', emit)
    if cleanup_failure == "group":
        monkeypatch.setattr(helper_process.SupervisedProcess, "terminate", uncertain)
    write_start(writer, ProcessSpec((sys.executable, "-c", "import signal;signal.pause()")), ())
    try:
        _run_terminated(session, fails=True)
        snapshot = _terminal_event(events)
        assert not snapshot.cleanup.confirmed
        assert (
            "child_group" if cleanup_failure == "group" else "address_lease"
        ) in snapshot.cleanup.residual_resources
        assert "workspace" in snapshot.cleanup.residual_resources
        assert "child_relays" not in snapshot.cleanup.residual_resources
        assert session.work.is_dir()
        assert (session.work / workspace_module.UNCONFIRMED_CHILD).is_file()
        assert session.workspace_lock.closed
        assert children[0].process.returncode is not None
        assert children[0].process.stdout.closed and children[0].process.stderr.closed
    finally:
        if lease is not None:
            lease.close()


@pytest.mark.parametrize("boundary", ("ready", "freeze"))
def test_latched_signal_before_commit_is_accounted(coordinated_helper, monkeypatch, boundary):
    session, writer, events, children = coordinated_helper
    if boundary == "ready":
        poll = helper_process.SupervisedProcess.poll
        injected = False

        def poll_then_signal(child):
            nonlocal injected
            result = poll(child)
            if not injected:
                injected = True
                session.handle_signal(signal.SIGTERM)
            return result

        monkeypatch.setattr(helper_process.SupervisedProcess, "poll", poll_then_signal)
        write_start(writer, ProcessSpec((sys.executable, "-c", "import signal;signal.pause()")), ())
    else:
        cleanup_report = session._cleanup_report

        def report_then_signal():
            report = cleanup_report()
            session.handle_signal(signal.SIGTERM)
            return report

        monkeypatch.setattr(session, "_cleanup_report", report_then_signal)
        writer.close()
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.primary_failure is not None
    assert snapshot.outcome.primary_failure.code == "REMOTE_SIGNAL"
    assert not any(kind == "READY" for kind, _ in events)
    assert snapshot.cleanup.confirmed
    assert all(child.process.returncode is not None for child in children)


@pytest.mark.parametrize("boundary", ("ready", "freeze"))
def test_native_signal_delivery_linearizes_after_blocked_commit(
    coordinated_helper, monkeypatch, boundary
):
    session, writer, events, _children = coordinated_helper
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())
    if boundary == "ready":
        emit = helper_streams.emit

        def signal_then_emit(kind, **fields):
            if kind == "READY":
                signal.raise_signal(signal.SIGTERM)
                assert session._signal_scope.pending_signum is None
            emit(kind, **fields)

        monkeypatch.setattr(helper_streams, 'emit', signal_then_emit)
        write_start(writer, ProcessSpec((sys.executable, "-c", "import signal;signal.pause()")), ())
    else:
        freeze = session.lifecycle.freeze

        def signal_then_freeze(cleanup):
            signal.raise_signal(signal.SIGTERM)
            assert session._signal_scope.pending_signum is None
            return freeze(cleanup)

        monkeypatch.setattr(session.lifecycle, "freeze", signal_then_freeze)
        writer.close()
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert signal.pthread_sigmask(signal.SIG_BLOCK, ()) == previous_mask
    assert session._signal_scope.pending_signum == signal.SIGTERM
    if boundary == "ready":
        assert any(kind == "READY" for kind, _ in events)
        assert snapshot.outcome.primary_failure.code == "REMOTE_SIGNAL"
    else:
        assert snapshot.outcome.primary_failure is None
        state = session.lifecycle.state
        assert isinstance(state, Closed)
        assert state.local_diagnostics[0].code == "REMOTE_SIGNAL"
    assert snapshot.cleanup.confirmed


def test_native_signal_after_frozen_result_fails_without_second_terminal(
    tmp_path, monkeypatch, control_pipe
):
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    _, writer = control_pipe
    writer.close()
    run = helper_session.HelperSession.run
    emit = helper_streams.emit
    events = []

    def run_then_signal(session):
        run(session)
        # The allocation wrapper still owns the native handlers at this point.
        signal.raise_signal(signal.SIGTERM)

    def record(kind, **fields):
        events.append((kind, fields))
        emit(kind, **fields)

    monkeypatch.setattr(helper_session.HelperSession, "run", run_then_signal)
    monkeypatch.setattr(helper_streams, 'emit', record)
    with pytest.raises(SystemExit) as raised:
        helper_control.control()
    assert raised.value.code == 1
    snapshot = _terminal_event(events)
    assert snapshot.outcome.trigger == Trigger.CONTROLLER_EOF
    assert snapshot.outcome.primary_failure is None
    assert snapshot.cleanup.confirmed
    assert not any(tmp_path.iterdir())


def test_terminal_partial_write_failure_does_not_replay_frozen_snapshot(
    tmp_path, monkeypatch, control_pipe
):
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    session = helper_session.HelperSession.create()
    _, writer = control_pipe
    writer.close()
    read_fd, write_fd = os.pipe()
    write = os.write
    emit = helper_streams.emit
    terminals = []
    partial = False

    def fail_after_prefix(descriptor, payload):
        nonlocal partial
        if descriptor == write_fd:
            if partial:
                raise BrokenPipeError("terminal prefix already delivered")
            if bytes(payload).startswith(b'{"child_result"'):
                partial = True
                return write(descriptor, payload[:5])
        return write(descriptor, payload)

    def record(kind, **fields):
        if kind == "SESSION_ENDED":
            terminals.append(fields)
        emit(kind, **fields)

    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "w", encoding="utf-8") as stdout:
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(os, "write", fail_after_prefix)
        monkeypatch.setattr(helper_streams, 'emit', record)
        _run_terminated(session, fails=True)
    assert partial
    assert len(terminals) == 1
    state = session.lifecycle.state
    assert isinstance(state, Closed)
    assert state.snapshot.cleanup.confirmed
    assert state.local_diagnostics
    assert session.workspace_lock.closed and not session.work.exists()


def test_workspace_cleanup_timeout_closes_admission_without_removing_live_stage(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    session_id, workspace, owner_lock = workspace_module.new_workspace()
    session = helper_session.HelperSession(session_id, workspace, owner_lock)
    clock = 0.0
    original_flock = workspace_module.fcntl.flock

    def observe_lease_contention(stream, operation):
        nonlocal clock
        try:
            return original_flock(stream, operation)
        except BlockingIOError:
            # Advance at the observed contention, not by scheduling or sleeping.
            clock = workspace_module.WORKSPACE_LEASE_TIMEOUT + 1.0
            raise

    monkeypatch.setattr(helper_process.time, "monotonic", lambda: clock)
    monkeypatch.setattr(workspace_module.fcntl, "flock", observe_lease_contention)
    with workspace_module._stage_lease(workspace):
        session._release_workspace()
        assert workspace.is_dir()
        assert len(session.cleanup_errors) == 1
        assert isinstance(session.cleanup_errors[0], TimeoutError)
        with pytest.raises(ValueError), workspace_module._stage_lease(workspace):
            pytest.fail("cleanup admitted a new stage")
    # Closure survives the existing lease's release and a failed removal.
    with pytest.raises(ValueError), workspace_module._stage_lease(workspace):
        pytest.fail("failed cleanup reopened staging")
    workspace_module.remove_workspace(workspace)
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("failed_suffixes", (("lease",), ("closed",), ("lease", "closed")))
def test_metadata_removal_failure_fails_session_and_is_reclaimed_later(
    tmp_path, monkeypatch, control_pipe, capsys, failed_suffixes
):
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    session_id, workspace, owner_lock = workspace_module.new_workspace()
    session = helper_session.HelperSession(session_id, workspace, owner_lock)
    metadata = {tmp_path / f".{session_id}.{suffix}" for suffix in failed_suffixes}
    path_type = type(tmp_path)
    original_unlink = path_type.unlink

    def fail_metadata_removal(path, *args, **kwargs):
        if path in metadata:
            raise PermissionError(f"cannot remove {path.name}")
        return original_unlink(path, *args, **kwargs)

    _reader, writer = control_pipe
    writer.close()
    with monkeypatch.context() as patch:
        patch.setattr(path_type, "unlink", fail_metadata_removal)
        with pytest.raises(SystemExit):
            session.run()

    assert not workspace.exists()
    assert owner_lock.closed
    assert set(tmp_path.iterdir()) == metadata
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "SESSION_ENDED"
    snapshot = decode_terminal(events[-1])
    assert snapshot.cleanup.residual_resources == ("workspace_metadata",)
    assert snapshot.outcome.primary_failure is not None
    assert all(suffix in repr(snapshot.outcome) for suffix in failed_suffixes)

    for path in metadata:
        os.utime(path, (1.0, 1.0))
    workspace_module.reclaim_stale_workspaces(tmp_path, now=workspace_module.STALE_SESSION_AGE + 2)
    assert tuple(tmp_path.iterdir()) == ()


def test_cleanup_closes_admission_without_waiting_for_global_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    first_id, first, first_owner = workspace_module.new_workspace()
    second_id, second, second_owner = workspace_module.new_workspace()
    first_session = helper_session.HelperSession(first_id, first, first_owner)
    second_session = helper_session.HelperSession(second_id, second, second_owner)
    clock = 0.0
    original_flock = workspace_module.fcntl.flock

    def bounded_flock(stream, operation):
        nonlocal clock
        # Fail a blocking acquisition at the OS boundary instead of deadlocking
        # unrelated allocation must proceed while this owner is stopped.
        if operation == fcntl.LOCK_EX:
            raise AssertionError("cleanup attempted an unbounded lock acquisition")
        try:
            return original_flock(stream, operation)
        except BlockingIOError:
            clock += workspace_module.WORKSPACE_LEASE_TIMEOUT + 1.0
            raise

    with workspace_module._stage_lease(first):
        with (tmp_path / ".workspace.lock").open("a+b") as old_gate:
            original_flock(old_gate, fcntl.LOCK_EX)
            monkeypatch.setattr(workspace_module.fcntl, "flock", bounded_flock)
            monkeypatch.setattr(helper_process.time, "monotonic", lambda: clock)
            first_session._release_workspace()
            assert len(first_session.cleanup_errors) == 1
            assert isinstance(first_session.cleanup_errors[0], TimeoutError)
            with pytest.raises(ValueError), workspace_module._stage_lease(first):
                pytest.fail("timed-out cleanup left admission open")
            second_session._release_workspace()
            assert not second_session.cleanup_errors
            assert not second.exists()
        assert first.is_dir()
    workspace_module.remove_workspace(first)


def test_address_lease_cleanup_failure_preserves_startup_failure(coordinated_helper, monkeypatch):
    session, writer, events, children = coordinated_helper
    record = helper_streams.emit
    lease = None

    class FailedLease:
        def close(self):
            raise OSError("address lease remains owned")

    def emit(kind, **fields):
        nonlocal lease
        record(kind, **fields)
        if kind == "ATTEMPT":
            lease = session._address_lease
            monkeypatch.setattr(session, "_address_lease", FailedLease())

    monkeypatch.setattr(helper_streams, 'emit', emit)
    write_start(
        writer,
        ProcessSpec(
            (
                sys.executable,
                "-c",
                "import sys;print('Address already in use',file=sys.stderr,flush=True);sys.exit(1)",
            ),
            required_output_sentinels=("not-ready",),
        ),
        (),
    )
    try:
        _run_terminated(session, fails=True)
        snapshot = _terminal_event(events)
        assert len(children) == 1
        assert sum(kind == "ATTEMPT" for kind, _fields in events) == 1
        assert snapshot.outcome.primary_failure is not None
        assert snapshot.outcome.primary_failure.code == "STARTUP_EXIT"
        assert "address_lease" in snapshot.cleanup.residual_resources
        assert session.work.is_dir() and session.workspace_lock.closed
    finally:
        if lease is not None:
            lease.close()
