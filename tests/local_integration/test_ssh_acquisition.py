# SPDX-License-Identifier: Apache-2.0

"""Physical process ownership across native creation and project handoffs."""

from __future__ import annotations

import dis
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import BinaryIO, cast

import pytest
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.deployment import DeploymentResult
from zephyr_remote_openocd.controller.forwarding import ForwardManager
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.ssh import SshCommand, _stop_process
from zephyr_remote_openocd.controller.waiting import wait_for

from tests.process_support import read_line

pytestmark = pytest.mark.local


@pytest.mark.parametrize("caller", ("helper", "forwarding"))
@pytest.mark.parametrize("failure_type", (MemoryError, SystemExit))
def test_transport_return_failure_cleans_callers_registered_process(
    tmp_path, monkeypatch, notified_authority, caller, failure_type
):
    launcher = tmp_path / "configured-ssh"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, shlex, sys\n"
        "remote = shlex.split(sys.argv[-1])\n"
        "os.execv(sys.executable, [sys.executable, *remote[1:]])\n"
    )
    launcher.chmod(0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    command = SshCommand((str(launcher),))
    helper_path = (
        Path(__file__).resolve().parents[2] / "python/zephyr_remote_openocd/helper/__main__.py"
    )
    authority = notified_authority.authority
    helper = HelperConnection(
        command,
        "unused-host",
        DeploymentResult(str(helper_path), "digest", False),
        authority=authority,
        notifier=notified_authority.notifier,
    )
    forwards = ForwardManager(command, "unused-host")
    failure = failure_type("transport caller handoff failed")
    processes = []
    pipes: list[BinaryIO | None] = []
    completion_descriptors: list[int] = []
    managed = []
    previous_trace = sys.gettrace()
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

    def interrupt_at_return(frame, event, arg):
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            process = frame.f_locals["self"]
            processes.append(process)
            pipes.extend((process.stdin, process.stdout, process.stderr))
        if event == "return" and frame.f_code is SshCommand.popen.__code__:
            managed.append(arg)
            if caller == "helper" and helper._completion.descriptor is not None:
                completion_descriptors.append(helper._completion.descriptor)
            raise failure
        return interrupt_at_return

    try:
        sys.settrace(interrupt_at_return)
        try:
            with pytest.raises(failure_type) as raised:
                if caller == "helper":
                    try:
                        helper.acquire()
                    finally:
                        helper.close()
                else:
                    forwards.start((ForwardSpec("gdb", 32961, 3333),), "127.64.0.1")
        finally:
            sys.settrace(previous_trace)

        assert raised.value is failure
        assert len(processes) == 1 and len(managed) == 1
        assert processes[0].returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)
        assert all(stream is not None and stream.closed for stream in pipes)
        for descriptor in completion_descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == previous_mask
        assert not forwards.has_forwards
        if caller == "helper":
            assert authority.snapshot().failure is failure
            root = tmp_path / "zephyr_remote_openocd-sessions-v1"
            assert not root.exists() or not tuple(root.iterdir())
    finally:
        sys.settrace(previous_trace)
        helper.close()
        forwards.close()
        for transport in managed:
            _stop_process(transport)


@pytest.mark.parametrize("search_path", (False, True), ids=("absolute", "search-path"))
@pytest.mark.parametrize("setup_failure", (False, True), ids=("latched", "primary-error"))
def test_native_sigint_before_pid_publication_is_latched_without_breaking_ownership(
    tmp_path, monkeypatch, search_path, setup_failure
):
    processes: list[subprocess.Popen[bytes]] = []
    child_pid = None
    requested = threading.Event()
    delivered = threading.Event()
    stopping = threading.Event()
    previous_trace = sys.gettrace()
    caller_frame = sys._getframe()
    previous_opcodes = caller_frame.f_trace_opcodes
    failure = MemoryError("constructor return failed")
    pid_stores = {
        method.__code__: {
            instruction.offset
            for instruction in dis.get_instructions(method)
            if instruction.opname == "STORE_ATTR" and instruction.argval == "pid"
        }
        for method in (
            subprocess.Popen._execute_child,  # type: ignore[attr-defined]
            subprocess.Popen._posix_spawn,  # type: ignore[attr-defined]
        )
    }
    assert all(pid_stores.values()), "runtime PID publication boundary was not found"

    def send_interrupt():
        # This thread predates launch and has SIGINT unblocked, so native
        # delivery is not deferred by the launch thread's inherited mask.
        if requested.wait(30) and not stopping.is_set():
            os.kill(os.getpid(), signal.SIGINT)
        delivered.set()

    def interrupt_at_publication(frame, event, _arg):
        nonlocal child_pid
        if frame.f_code in pid_stores:
            frame.f_trace_opcodes = True
            if event == "opcode" and frame.f_lasti in pid_stores[frame.f_code] and not processes:
                process = frame.f_locals["self"]
                processes.append(process)
                assert process.pid is None
                # The real child acknowledges execution independently of stdin.
                child_pid = int(read_line(process.stdout).strip())
                requested.set()
                assert delivered.wait(30), "native signal was not sent"
        if (
            setup_failure
            and processes
            and event == "return"
            and frame.f_code is subprocess.Popen.__init__.__code__
        ):
            raise failure
        return interrupt_at_publication

    executable = sys.executable
    if search_path:
        # Bare executable lookup exercises fork/exec on runtimes that select
        # posix_spawn for absolute paths, without replacing either backend.
        executable = "transport-python"
        (tmp_path / executable).symlink_to(sys.executable)
        monkeypatch.setenv("PATH", str(tmp_path))
    command = SshCommand(
        (executable, "-c", "import os, signal; print(os.getpid(), flush=True); signal.pause()")
    )
    sender = threading.Thread(target=send_interrupt)
    sender.start()
    managed = None
    try:
        with pytest.raises((OperationCancelled, MemoryError)) as raised, operation_scope() as scope:
            # Python 3.12 enables opcode events only if a frame requests them
            # before settrace; restore the caller's setting with its trace.
            caller_frame.f_trace_opcodes = True
            sys.settrace(interrupt_at_publication)
            try:
                managed = command.popen("unused-host", "unused-command")
            finally:
                sys.settrace(previous_trace)
                caller_frame.f_trace_opcodes = previous_opcodes
            # The handler only latched: acquisition completed into its owner,
            # and the cancellation is dispatched at the next wait boundary.
            assert scope.requested
            wait_for()

        assert len(processes) == 1 and child_pid is not None
        process = processes[0]
        assert process.pid == child_pid
        if setup_failure:
            assert raised.value is failure
            assert not any("SIGINT" in note for note in failure.__notes__)
            # Rollback disposed of the transport before the error propagated.
            assert process.returncode is not None
            with pytest.raises(ChildProcessError):
                os.waitpid(child_pid, os.WNOHANG)
            assert all(
                stream is not None and stream.closed
                for stream in (process.stdin, process.stdout, process.stderr)
            )
        else:
            assert isinstance(raised.value, OperationCancelled)
            assert managed is not None and managed.pid == child_pid
            _stop_process(managed)
            assert managed.returncode is not None
    finally:
        sys.settrace(previous_trace)
        caller_frame.f_trace_opcodes = previous_opcodes
        stopping.set()
        requested.set()
        sender.join(30)
        assert not sender.is_alive()
        if child_pid is not None:
            try:
                waited_pid, _status = os.waitpid(child_pid, os.WNOHANG)
            except ChildProcessError:
                pass
            else:
                if waited_pid == 0:
                    os.kill(child_pid, signal.SIGKILL)
                    os.waitpid(child_pid, 0)
        for process in processes:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


@pytest.mark.parametrize("failure_type", (MemoryError, SystemExit))
def test_popen_return_interruption_reaps_transport(failure_type):
    processes: list[subprocess.Popen[bytes]] = []
    previous_trace = sys.gettrace()
    failure = failure_type("Popen return interrupted")

    def interrupt_at_return(frame: FrameType, event: str, _arg: object):
        if event == "return" and frame.f_code is subprocess.Popen.__init__.__code__:
            process = cast(subprocess.Popen[bytes], frame.f_locals["self"])
            processes.append(process)
            assert process.pid is not None
            raise failure
        return interrupt_at_return

    try:
        sys.settrace(interrupt_at_return)
        try:
            with pytest.raises(failure_type) as raised:
                SshCommand((sys.executable, "-c", "import sys; sys.stdin.buffer.read()")).popen(
                    "unused-host", "unused-command"
                )
        finally:
            sys.settrace(previous_trace)

        assert raised.value is failure
        assert len(processes) == 1
        process = processes[0]
        # Inspect before test-owned fallback cleanup: production must have
        # terminated, reaped, and closed every acquired pipe itself.
        assert process.returncode is not None
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
        assert all(
            stream is not None and stream.closed
            for stream in (process.stdin, process.stdout, process.stderr)
        )
    finally:
        sys.settrace(previous_trace)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


@pytest.mark.timeout(120)
def test_stderr_reader_start_return_failure_keeps_bounded_drain_shutdown(tmp_path) -> None:
    descendant_ready = tmp_path / "descendant-ready"
    os.mkfifo(descendant_ready)
    # A descendant inherits stderr, so the drain cannot reach EOF on its own.
    descendant = (
        "import os, signal, sys\n"
        f"with open({str(descendant_ready)!r}, 'w') as ready:\n"
        "    ready.write(str(os.getpid()))\n"
        "signal.pause()\n"
    )
    code = (
        f"import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
        "sys.stdin.buffer.read()\n"
    )
    failure = MemoryError("stderr reader start return failed")
    drains = []
    descendants: list[int] = []
    previous_trace = sys.gettrace()

    def fail_after_start(frame: FrameType, event: str, _arg: object):
        thread = frame.f_locals.get("self")
        if (
            event == "return"
            and frame.f_code is threading.Thread.start.__code__
            and isinstance(thread, threading.Thread)
            and thread.name == "zro-ssh-stderr"
        ):
            drains.append(thread._target.__self__)  # type: ignore[attr-defined]
            with descendant_ready.open() as ready:
                descendants.append(int(ready.read()))
            # The reader is running and stderr has a live inherited writer; the
            # start call is interrupted as it returns.
            raise failure
        return fail_after_start

    try:
        sys.settrace(fail_after_start)
        try:
            with pytest.raises(MemoryError) as raised:
                SshCommand((sys.executable, "-c", code)).popen("host", "serve")
        finally:
            sys.settrace(previous_trace)
        assert raised.value is failure
        # Rollback reported the unstoppable reader instead of closing a buffered
        # stream that the reader may hold.
        notes = getattr(failure, "__notes__", ())
        assert any("stderr reader did not stop" in note for note in notes)
    finally:
        sys.settrace(previous_trace)
        for pid in descendants:
            os.kill(pid, signal.SIGKILL)
        for drain in drains:
            drain.close()
