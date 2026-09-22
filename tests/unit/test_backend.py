# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import os
import threading
from typing import Any, cast

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote import forwarding as forwarding_module
from zephyr_remote_openocd.remote import helper_client as helper_client_module
from zephyr_remote_openocd.remote.backend import RemoteSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.forwarding import _ForwardManager
from zephyr_remote_openocd.remote.helper_client import _HelperClient, _HelperCloseResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
)
from zephyr_remote_openocd.remote.session import SessionClosedError, SessionError, _SessionState
from zephyr_remote_openocd.remote.ssh import SshCommand

OPENOCD_FAILURE_RC = 7
FORWARD_FAILURE_RC = 13


def test_open_rolls_back_failed_acquisition_once(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    startup_error = RuntimeError("staging failed")
    cleanup_calls = 0

    monkeypatch.setattr(backend_module, "deploy_helper", lambda *_args: deployment)

    def fail_stage(_session, _files):
        raise startup_error

    def close(_session):
        nonlocal cleanup_calls
        cleanup_calls += 1

    monkeypatch.setattr(_HelperClient, "open", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(RemoteSession, "_stage", fail_stage)
    monkeypatch.setattr(RemoteSession, "close", close)

    with pytest.raises(RuntimeError) as raised:
        RemoteSession.open(request)

    assert raised.value is startup_error
    assert cleanup_calls == 1


def test_open_retains_nested_rollback_cleanup_diagnostics(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    startup_error = RuntimeError("staging failed")
    cleanup_error = RuntimeError("session cleanup failed")
    cleanup_error.add_note("additional cleanup failure: forward cleanup failed")

    monkeypatch.setattr(backend_module, "deploy_helper", lambda *_args: deployment)
    monkeypatch.setattr(_HelperClient, "open", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        RemoteSession,
        "_stage",
        lambda _session, _files: (_ for _ in ()).throw(startup_error),
    )

    def fail_close(_session):
        raise cleanup_error

    monkeypatch.setattr(RemoteSession, "close", fail_close)

    with pytest.raises(RuntimeError) as raised:
        RemoteSession.open(request)

    assert raised.value is startup_error
    notes = raised.value.__notes__
    assert any("session cleanup failed" in note for note in notes)
    assert any("forward cleanup failed" in note for note in notes)
    assert all("startup failure cleanup also failed" in note for note in notes)


def test_helper_open_retains_nested_cleanup_diagnostics(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    cleanup_error = RuntimeError("helper process cleanup failed")
    cleanup_error.add_note("process cleanup also failed: stream close failed")

    class Process:
        stdout = None

    def fail_stop(_process, *, close_streams=True):
        del close_streams
        raise cleanup_error

    monkeypatch.setattr(SshCommand, "popen", lambda *_args: Process())
    monkeypatch.setattr(helper_client_module, "_stop_process", fail_stop)

    with pytest.raises(SessionError) as raised:
        _HelperClient.open(request.ssh_command, request.host, deployment)

    assert raised.value is not cleanup_error
    notes = raised.value.__notes__
    assert any("helper process cleanup failed" in note for note in notes)
    assert any("stream close failed" in note for note in notes)
    assert all("helper startup cleanup also failed" in note for note in notes)


def test_closed_session_exposes_only_cached_openocd_result():
    class Helper:
        openocd_returncode = None

    session = cast(Any, object.__new__(RemoteSession))
    session.closed = True
    session._helper = Helper()

    assert session.openocd_returncode is None
    assert session.check_openocd_exit() is None
    with pytest.raises(SessionClosedError):
        session.wait_for_openocd_exit()
    with pytest.raises(SessionClosedError):
        session.forward(())

    completed = cast(Any, object.__new__(RemoteSession))
    completed.closed = True
    completed._helper = type("Helper", (), {"openocd_returncode": OPENOCD_FAILURE_RC})()
    assert completed.openocd_returncode == OPENOCD_FAILURE_RC
    assert completed.check_openocd_exit() == OPENOCD_FAILURE_RC
    assert completed.wait_for_openocd_exit() == OPENOCD_FAILURE_RC


def test_only_process_exit_terminal_event_sets_openocd_result():
    state = _SessionState()

    state.record_terminal("requested", None)
    assert state.openocd_returncode is None

    state.record_terminal("process_exit", OPENOCD_FAILURE_RC)
    assert state.openocd_returncode == OPENOCD_FAILURE_RC


def test_unexpected_requested_terminal_event_fails_status_observation():
    state = _SessionState()

    state.record_terminal("requested", None)

    with pytest.raises(SessionError):
        state.recorded_openocd_exit()


@pytest.mark.timeout(10)
def test_requested_stop_serializes_terminal_event_with_stop_write():
    terminal_attempted = threading.Event()
    stop_write_entered = threading.Event()
    release_stop_write = threading.Event()
    terminal_recorded = threading.Event()
    observe_terminal = threading.Event()

    class ObservableCondition(threading.Condition):
        def __enter__(self):
            if observe_terminal.is_set():
                terminal_attempted.set()
            return super().__enter__()

    state = _SessionState()
    state._changed = ObservableCondition(state._lock)

    def write_stop():
        stop_write_entered.set()
        assert release_stop_write.wait(5)

    stopper = threading.Thread(target=lambda: state.request_stop(write_stop))
    stopper.start()
    assert stop_write_entered.wait(5)

    def record_terminal():
        observe_terminal.set()
        state.record_terminal("requested", None)
        terminal_recorded.set()

    terminal = threading.Thread(target=record_terminal)
    terminal.start()
    assert terminal_attempted.wait(5)
    assert not terminal_recorded.is_set()

    release_stop_write.set()
    stopper.join(timeout=5)
    terminal.join(timeout=5)

    assert not stopper.is_alive()
    assert not terminal.is_alive()
    assert terminal_recorded.is_set()
    assert state.recorded_openocd_exit() is None


def test_helper_close_keeps_reader_owned_stdout_open_until_reader_stops():
    reader_stopped = threading.Event()

    class ReaderOwnedStream(io.BytesIO):
        def close(self):
            assert reader_stopped.is_set()
            super().close()

    class Reader:
        def join(self, timeout=None):
            del timeout

        @staticmethod
        def is_alive():
            return not reader_stopped.is_set()

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.returncode = None
            self.stdin = io.BytesIO()
            self.stdout = ReaderOwnedStream()
            self.stderr = io.BytesIO()

        def poll(self):
            return self.returncode

        def terminate(self):
            reader_stopped.set()
            self.returncode = 0

        def kill(self):
            reader_stopped.set()
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

        def close_stderr(self):
            self.stderr.close()

    helper = _HelperClient(SshCommand(), "host", DeploymentResult("/helper.py", "digest", False))
    test_helper = cast(Any, helper)
    test_helper._process = Process()
    test_helper._state.record_terminal("process_exit", 0)
    test_helper._reader_thread = Reader()

    assert helper.close().error is None

    assert reader_stopped.is_set()
    assert not test_helper._reader_thread.is_alive()
    assert test_helper._process.stdin.closed
    assert test_helper._process.stdout.closed
    assert test_helper._process.stderr.closed
    assert helper.close().error is None


def test_close_attempts_all_cleanup_once_and_preserves_first_failure():
    session = cast(Any, object.__new__(RemoteSession))
    session.closed = False
    first_error = RuntimeError("forward cleanup failed")
    later_error = RuntimeError("helper cleanup failed")
    later_error.add_note("helper cleanup also failed: stream close failed")
    actions = []

    def close_forwards():
        actions.append("forwards")
        raise first_error

    def close_helper():
        actions.append("helper")
        return _HelperCloseResult(later_error, ())

    session._forwards = type("Forwards", (), {"close": staticmethod(close_forwards)})()
    session._helper = type("Helper", (), {"close": staticmethod(close_helper)})()

    with pytest.raises(RuntimeError) as raised:
        session.close()

    assert raised.value is first_error
    notes = raised.value.__notes__
    assert any("helper cleanup failed" in note for note in notes)
    assert any("stream close failed" in note for note in notes)
    assert all("additional cleanup failure" in note for note in notes)
    assert set(actions) == {"forwards", "helper"}
    assert len(actions) == 2
    assert session.closed
    assert session.close() is None
    assert set(actions) == {"forwards", "helper"}
    assert len(actions) == 2


def test_helper_close_retains_nested_process_cleanup_diagnostics():
    terminate_error = RuntimeError("helper terminate failed")
    stderr_error = RuntimeError("helper stderr close failed")

    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            raise terminate_error

        def close_stderr(self):
            raise stderr_error

    helper = _HelperClient(SshCommand(), "host", DeploymentResult("/helper.py", "digest", False))
    test_helper = cast(Any, helper)
    test_helper._process = Process()
    test_helper._state.request_stop(lambda: None)
    test_helper._state.record_terminal("requested", None)

    result = helper.close()

    assert isinstance(result.error, SessionError)
    assert result.cleanup_errors == (terminate_error,)
    notes = result.error.__notes__
    assert any("helper terminate failed" in note for note in notes)
    assert any("helper stderr close failed" in note for note in notes)
    assert all("helper cleanup also failed" in note for note in notes)


@pytest.mark.timeout(10)
def test_session_state_wakes_on_terminal_event():
    waiting = threading.Event()

    class ObservableCondition(threading.Condition):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    state = _SessionState()
    state._changed = ObservableCondition(state._lock)
    results = []

    def wait_for_result():
        state.wait_for_change(None)
        results.append(state.recorded_openocd_exit())

    waiter = threading.Thread(target=wait_for_result)
    waiter.start()
    assert waiting.wait(5)
    state.record_terminal("process_exit", 0)
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert results == [0]


def test_forward_manager_raises_when_ssh_forward_exits():
    class Forward:
        def poll(self):
            return FORWARD_FAILURE_RC

        def stderr_tail(self):
            return b"forward failed"

    manager = _ForwardManager(SshCommand(), "host")
    manager._processes = [cast(Any, Forward())]

    with pytest.raises(SessionError):
        manager.check_health()


@pytest.mark.timeout(10)
def test_wait_for_openocd_exit_observes_forward_failure():
    class Helper:
        openocd_returncode = None

        def __init__(self, forwards):
            self.forwards = forwards
            self.wait_timeouts = []

        @staticmethod
        def recorded_openocd_exit():
            return None

        @staticmethod
        def has_result_or_reader_failure():
            return False

        def wait_for_change(self, timeout):
            self.wait_timeouts.append(timeout)
            self.forwards.failed = True

    class Forwards:
        has_forwards = True

        def __init__(self):
            self.failed = False

        def check_health(self):
            if self.failed:
                raise SessionError(f"SSH forwarding exited with status {FORWARD_FAILURE_RC}")

    session = cast(Any, object.__new__(RemoteSession))
    session.closed = False
    forwards = Forwards()
    session._helper = Helper(forwards)
    session._forwards = forwards

    with pytest.raises(SessionError):
        session.wait_for_openocd_exit()
    assert len(session._helper.wait_timeouts) == 1
    assert 0 < session._helper.wait_timeouts[0] <= backend_module.FORWARD_HEALTH_INTERVAL


@pytest.mark.timeout(5)
def test_forward_readiness_timeout_does_not_block_on_partial_output(monkeypatch):
    read_fd, write_fd = os.pipe()

    class Process:
        def __init__(self):
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)

        @staticmethod
        def poll():
            return None

    process = Process()
    try:
        os.write(write_fd, b"ZRO_FORWARD_")

        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

        class Selector:
            def __init__(self):
                self.delivered = False

            def register(self, _stream, _events):
                pass

            def select(self, _timeout):
                if not self.delivered:
                    self.delivered = True
                    clock.now = 1.0
                    return [(None, None)]
                return []

            def close(self):
                pass

        clock = Clock()
        monkeypatch.setattr(forwarding_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(forwarding_module.selectors, "DefaultSelector", Selector)
        deadline = 0.5
        assert not _ForwardManager._await_ready(cast(Any, process), "ZRO_FORWARD_ready", deadline)
    finally:
        process.stdout.close()
        os.close(write_fd)
