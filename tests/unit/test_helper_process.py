# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import io
import math
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from zephyr_remote_openocd.helper import _process as helper_process

from tests.helper_process_support import cleanup_supervised_process

SAMPLE_CHILD_EXIT_CODE = 7


def _wait_for_descendant(path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            value = path.read_text(encoding="ascii")
            if value:
                return int(value)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.01)
    raise AssertionError("descendant PID was not published")


def _assert_pidfd_exited(pidfd, timeout=5):
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    if not poller.poll(timeout * 1000):
        raise AssertionError("process survived cleanup")


def _forking_child_code(exit_on_term):
    leader_signal = "SIGTERM" if exit_on_term else "SIGUSR1"
    leader_exit = f"""
def stop(_signal, _frame):
    raise SystemExit(0)

signal.signal(signal.{leader_signal}, stop)
"""
    parent_wait = "time.sleep(30)" if exit_on_term else "signal.pause()"
    return f"""
import os
import signal
import sys
import time

{leader_exit}
descendant = os.fork()
if descendant == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(descriptor, str(os.getpid()).encode("ascii"))
    os.close(descriptor)
    os.close(1)
    os.close(2)
    time.sleep(30)
{parent_wait}
"""


@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_spawn_child_rolls_back_process_when_ownership_wrapper_fails(monkeypatch, cleanup_fails):
    initialize = subprocess.Popen.__init__
    processes = []
    process_pidfds = []
    failure = RuntimeError("injected child ownership failure")
    cleanup_errors = [OSError(f"child {stream} closure failed") for stream in ("stdout", "stderr")]

    def fail_close(stream, cleanup_error):
        close = stream.close

        def close_then_fail():
            close()
            raise cleanup_error

        monkeypatch.setattr(stream, "close", close_then_fail)

    def capture_popen(process, *args, **kwargs):
        initialize(process, *args, **kwargs)
        processes.append(process)
        process_pidfds.append(os.pidfd_open(process.pid))
        if cleanup_fails:
            for stream, cleanup_error in zip(
                (process.stdout, process.stderr), cleanup_errors, strict=True
            ):
                fail_close(stream, cleanup_error)

    def fail_child_ownership(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(subprocess.Popen, "__init__", capture_popen)
    monkeypatch.setattr(helper_process, 'SupervisedProcess', fail_child_ownership)
    try:
        with pytest.raises(RuntimeError) as raised:
            helper_process.spawn_child((sys.executable, "-c", "import signal; signal.pause()"))

        assert raised.value is failure
        assert len(processes) == 1
        _assert_pidfd_exited(process_pidfds[0])
        assert processes[0].returncode is not None
        assert processes[0].stdout is not None and processes[0].stdout.closed
        assert processes[0].stderr is not None and processes[0].stderr.closed
        if cleanup_fails:
            for cleanup_error in cleanup_errors:
                assert any(str(cleanup_error) in note for note in failure.__notes__)
    finally:
        for process in processes:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with suppress(BaseException):
                    process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        for process_pidfd in process_pidfds:
            os.close(process_pidfd)


@pytest.mark.timeout(10)
def test_spawn_rollback_preserves_exited_leader_identity_until_group_signal(monkeypatch):
    initialize = subprocess.Popen.__init__
    killpg = os.killpg
    failure = RuntimeError("supervisor construction failed after child exit")
    owner = helper_process.ChildAcquisition()
    signalled = []

    def exited_process(process, *args, **kwargs):
        initialize(process, *args, **kwargs)
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)

    def fail_construction(*_args, **_kwargs):
        raise failure

    def signal_owned_group(pid, signum):
        # Reaping before this boundary would release the identity and make this
        # observation fail, even though the acquisition ticket still owns it.
        if signum != 0:
            status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            assert status is not None and status.si_status == SAMPLE_CHILD_EXIT_CODE
            signalled.append(pid)
        killpg(pid, signum)

    monkeypatch.setattr(subprocess.Popen, "__init__", exited_process)
    monkeypatch.setattr(helper_process, 'SupervisedProcess', fail_construction)
    monkeypatch.setattr(os, "killpg", signal_owned_group)
    with pytest.raises(RuntimeError) as raised:
        helper_process.spawn_child(
            (sys.executable, "-c", f"import sys;sys.exit({SAMPLE_CHILD_EXIT_CODE})"),
            ownership=owner,
        )
    assert raised.value is failure
    assert owner.process is not None
    assert signalled == [owner.process.pid]
    assert owner.process.returncode == SAMPLE_CHILD_EXIT_CODE
    assert owner.rollback_confirmed and owner.producer_quiescent
    assert not owner.termination_requested
    assert owner.process.stdout is not None and owner.process.stdout.closed
    assert owner.process.stderr is not None and owner.process.stderr.closed


def test_supervised_child_terminates_descendant_after_leader_term(tmp_path):
    descendant_path = tmp_path / "descendant.pid"
    child = helper_process.spawn_child(
        (sys.executable, "-c", _forking_child_code(True), str(descendant_path))
    )
    descendant_pid = None
    descendant_pidfd = None
    try:
        descendant_pid = _wait_for_descendant(descendant_path)
        descendant_pidfd = os.pidfd_open(descendant_pid)
        assert os.getpgid(descendant_pid) == child.pid
        assert child.poll() is None

        asyncio.run(child.terminate())
        child.close_streams()

        assert child.returncode == 0
        assert child.termination_requested
        assert child.group_disposed
        _assert_pidfd_exited(descendant_pidfd)
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, 0)
    finally:
        cleanup_supervised_process(child, descendant_pidfd)
        if descendant_pidfd is not None:
            os.close(descendant_pidfd)


def test_supervised_child_warns_and_terminates_descendant_after_leader_exit(tmp_path, capfd):
    descendant_path = tmp_path / "descendant.pid"
    child = helper_process.spawn_child(
        (sys.executable, "-c", _forking_child_code(False), str(descendant_path))
    )
    descendant_pid = None
    descendant_pidfd = None
    leader_pidfd = None
    try:
        descendant_pid = _wait_for_descendant(descendant_path)
        descendant_pidfd = os.pidfd_open(descendant_pid)
        leader_pidfd = os.pidfd_open(child.pid)
        assert os.getpgid(descendant_pid) == child.pid

        os.kill(child.pid, signal.SIGUSR1)
        _assert_pidfd_exited(leader_pidfd)
        assert child.process.returncode is None

        asyncio.run(child.terminate())
        child.close_streams()

        assert child.returncode == 0
        assert not child.termination_requested
        assert child.group_disposed
        _assert_pidfd_exited(descendant_pidfd)
        assert str(descendant_pid) in capfd.readouterr().err
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, 0)
    finally:
        cleanup_supervised_process(child, descendant_pidfd)
        if descendant_pidfd is not None:
            os.close(descendant_pidfd)
        if leader_pidfd is not None:
            os.close(leader_pidfd)


def test_supervised_child_skips_kill_after_group_disappears(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self.wait_calls = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = 0
            return self.returncode

    process = Process()
    signals = []

    def killpg(_pid, signum):
        signals.append(signum)
        if signum == 0:
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg)
    child = helper_process.SupervisedProcess(process)
    monkeypatch.setattr(
        os,
        "waitid",
        lambda *_args: SimpleNamespace(si_pid=process.pid, si_code=os.CLD_EXITED, si_status=0),
    )
    assert child.poll() == 0

    asyncio.run(child.terminate())

    assert signal.SIGTERM in signals
    assert signal.SIGKILL not in signals
    assert process.wait_calls
    assert all(timeout is not None for timeout in process.wait_calls)


def test_supervised_child_reaps_and_cleans_up_after_signal_errors(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self.wait_calls = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = 0
            return self.returncode

    process = Process()
    child = helper_process.SupervisedProcess(process)
    monkeypatch.setattr(
        os,
        "waitid",
        lambda *_args: SimpleNamespace(si_pid=process.pid, si_code=os.CLD_EXITED, si_status=0),
    )
    assert child.poll() == 0
    term_error = RuntimeError("term failed")

    def fail_term(_pid, signum):
        if signum == signal.SIGTERM:
            raise term_error
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", fail_term)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(child.terminate())
        child.close_streams()

    assert raised.value is term_error
    assert process.wait_calls
    assert all(timeout is not None for timeout in process.wait_calls)


def test_supervised_child_group_cleanup_ignores_diagnostic_failure(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def wait(self, timeout=None):
            self.returncode = -signal.SIGKILL
            return self.returncode

    child = helper_process.SupervisedProcess(Process())
    monkeypatch.setattr(os, "waitid", lambda *_args: None)
    signals = []

    def killpg(_pid, signum):
        signals.append(signum)
        if signum == 0 and child.process.returncode is not None:
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg)

    async def leader_exited():
        return True

    monkeypatch.setattr(child, "_wait_for_leader_exit", leader_exited)
    monkeypatch.setattr(
        child,
        "_remaining_group_members",
        lambda: (_ for _ in ()).throw(OSError("proc unavailable")),
    )

    asyncio.run(child.terminate())

    assert signals.count(signal.SIGTERM) == 1
    assert signals.count(signal.SIGKILL) == 1
    assert signals.index(signal.SIGTERM) < signals.index(signal.SIGKILL)


@pytest.fixture
def cleanup_process(monkeypatch):
    process = create_autospec(subprocess.Popen, instance=True)
    process.pid = 123
    process.returncode = None
    process.stdout = io.BytesIO()
    process.stderr = io.BytesIO()
    waitid = os.waitid

    def observe_owned_process(kind, pid, options):
        if kind == os.P_PID and pid == process.pid:
            return None
        return waitid(kind, pid, options)

    monkeypatch.setattr(os, "waitid", observe_owned_process)

    def reap(timeout=None):
        assert timeout is not None and math.isfinite(timeout) and timeout > 0
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.wait.side_effect = reap
    return process


def _run_group_cleanup(process, mode, monkeypatch):
    if mode == "rollback":
        helper_process.rollback_spawned_process(process)
    else:
        child = helper_process.SupervisedProcess(process)
        monkeypatch.setattr(
            os,
            "waitid",
            lambda *_args: SimpleNamespace(si_pid=process.pid, si_code=os.CLD_EXITED, si_status=0),
        )
        assert child.poll() == 0
        try:
            asyncio.run(child.terminate())
        finally:
            child.close_streams()


@pytest.mark.parametrize("mode", ("supervised", "rollback"))
@pytest.mark.parametrize("observation", ("exists", "late-exit", "permission-denied"))
def test_group_cleanup_fails_when_disappearance_is_unconfirmed(
    monkeypatch, cleanup_process, mode, observation
):
    now = 0.0
    waits = []
    group_gone = False

    def advance(delay):
        nonlocal now, group_gone
        assert math.isfinite(delay) and delay > 0
        waits.append(delay)
        if observation == "late-exit":
            # The final member exits, but observation resumes after expiry.
            now += helper_process.CHILD_GROUP_EXIT_TIMEOUT + delay
            group_gone = True
        else:
            now += delay

    async def async_advance(delay):
        advance(delay)

    def killpg(pid, signum):
        assert pid == cleanup_process.pid
        if cleanup_process.returncode is not None:
            # No further signalling is safe once reaping releases the PID.
            assert signum == 0
            if observation == "permission-denied":
                raise PermissionError("group cannot be inspected")
            if group_gone:
                raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(
        helper_process, "time", SimpleNamespace(monotonic=lambda: now, sleep=advance)
    )
    monkeypatch.setattr(asyncio, "sleep", async_advance)

    expected = PermissionError if observation == "permission-denied" else TimeoutError
    with pytest.raises(expected):
        _run_group_cleanup(cleanup_process, mode, monkeypatch)

    assert cleanup_process.returncode is not None
    assert cleanup_process.stdout.closed and cleanup_process.stderr.closed
    if observation == "exists":
        assert waits
        assert now <= helper_process.CHILD_GROUP_EXIT_TIMEOUT


@pytest.mark.parametrize("mode", ("supervised", "rollback"))
def test_group_cleanup_waits_for_disappearance_after_reaping(monkeypatch, cleanup_process, mode):
    group_gone = False
    now = 0.0

    def finish_group_exit(delay):
        nonlocal group_gone, now
        assert cleanup_process.returncode is not None
        assert math.isfinite(delay) and delay > 0
        # Model the last member being reaped during the observation wait.
        group_gone = True
        now += delay

    async def async_finish_group_exit(delay):
        finish_group_exit(delay)

    def killpg(pid, signum):
        assert pid == cleanup_process.pid
        if cleanup_process.returncode is not None:
            assert signum == 0
        if group_gone:
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(
        helper_process, "time", SimpleNamespace(monotonic=lambda: now, sleep=finish_group_exit)
    )
    monkeypatch.setattr(asyncio, "sleep", async_finish_group_exit)

    _run_group_cleanup(cleanup_process, mode, monkeypatch)

    assert group_gone
    assert cleanup_process.returncode is not None


def test_supervised_child_cleanup_uses_finite_budgets_after_failures(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self.wait_calls = []

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            raise TimeoutError("reap failed")

    process = Process()
    child = helper_process.SupervisedProcess(process)
    monkeypatch.setattr(os, "waitid", lambda *_args: None)
    signal_error = RuntimeError("signal failed")
    signals = []

    def fail_signal(_pid, signum):
        signals.append(signum)
        if signum != 0:
            raise signal_error

    async def fail_leader_wait():
        raise RuntimeError("leader wait failed")

    now = 0.0

    async def advance(delay):
        nonlocal now
        assert math.isfinite(delay) and delay > 0
        now += delay

    monkeypatch.setattr(helper_process, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(asyncio, "sleep", advance)
    monkeypatch.setattr(os, "killpg", fail_signal)
    monkeypatch.setattr(child, "_wait_for_leader_exit", fail_leader_wait)
    monkeypatch.setattr(child, "_warn_remaining_group_members", lambda: None)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(child.terminate())

    assert raised.value is signal_error
    assert signal.SIGTERM in signals and signal.SIGKILL in signals
    assert signals.index(signal.SIGTERM) < signals.index(signal.SIGKILL)
    assert all(
        timeout is not None
        and math.isfinite(timeout)
        and 0 < timeout <= helper_process.CHILD_REAP_TIMEOUT
        for timeout in process.wait_calls
    )
    assert any("leader wait failed" in note for note in raised.value.__notes__)
    assert any("reap failed" in note for note in raised.value.__notes__)
