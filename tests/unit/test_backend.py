# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import os
import threading
from typing import Any, cast

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote.backend import RemoteSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
)
from zephyr_remote_openocd.remote.session import SessionClosedError, SessionError
from zephyr_remote_openocd.remote.ssh import SshCommand

OPENOCD_FAILURE_RC = 7
FORWARD_FAILURE_RC = 13


def test_open_rolls_back_failed_acquisition_once(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    startup_error = RuntimeError("staging failed")
    cleanup_calls = 0

    monkeypatch.setattr(backend_module, "deploy_helper", lambda *_args: deployment)

    def open_helper(session):
        session.helper_process = object()

    def fail_stage(_session, _files):
        raise startup_error

    def close(_session):
        nonlocal cleanup_calls
        cleanup_calls += 1

    monkeypatch.setattr(RemoteSession, "_open_helper", open_helper)
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
    monkeypatch.setattr(RemoteSession, "_open_helper", lambda _session: None)
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


def test_open_helper_retains_nested_cleanup_diagnostics(monkeypatch):
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
    monkeypatch.setattr(RemoteSession, "_stop_process", staticmethod(fail_stop))

    session = RemoteSession(request, deployment)

    with pytest.raises(SessionError) as raised:
        session._open_helper()

    assert raised.value is not cleanup_error
    notes = raised.value.__notes__
    assert any("helper process cleanup failed" in note for note in notes)
    assert any("stream close failed" in note for note in notes)
    assert all("helper startup cleanup also failed" in note for note in notes)


def test_closed_session_exposes_only_cached_openocd_result():
    session = cast(Any, object.__new__(RemoteSession))
    session.closed = True
    session._openocd_returncode = None

    assert session.openocd_returncode is None
    assert session.check_openocd_exit() is None
    with pytest.raises(SessionClosedError):
        session.wait_for_openocd_exit()
    with pytest.raises(SessionClosedError):
        session.forward(())

    completed = cast(Any, object.__new__(RemoteSession))
    completed.closed = True
    completed._openocd_returncode = OPENOCD_FAILURE_RC
    assert completed.openocd_returncode == OPENOCD_FAILURE_RC
    assert completed.check_openocd_exit() == OPENOCD_FAILURE_RC
    assert completed.wait_for_openocd_exit() == OPENOCD_FAILURE_RC


def test_only_process_exit_terminal_event_sets_openocd_result():
    session = cast(Any, object.__new__(RemoteSession))
    session.output_handler = None
    session._state_lock = threading.RLock()
    session._state_changed = threading.Condition(session._state_lock)
    session._terminal_reason = None
    session._stop_requested = False
    session._openocd_returncode = None

    session._dispatch({"type": "SESSION_CLOSED", "reason": "requested", "returncode": None})
    assert session.openocd_returncode is None

    session._dispatch(
        {
            "type": "SESSION_CLOSED",
            "reason": "process_exit",
            "returncode": OPENOCD_FAILURE_RC,
        }
    )
    assert session.openocd_returncode == OPENOCD_FAILURE_RC


def test_unexpected_requested_terminal_event_fails_status_observation():
    session = cast(Any, object.__new__(RemoteSession))
    session.closed = False
    session.output_handler = None
    session.reader_error = None
    session.forwards = []
    session._state_lock = threading.RLock()
    session._state_changed = threading.Condition(session._state_lock)
    session._terminal_reason = None
    session._stop_requested = False
    session._openocd_returncode = None

    session._dispatch({"type": "SESSION_CLOSED", "reason": "requested", "returncode": None})

    with pytest.raises(SessionError):
        session.wait_for_openocd_exit(timeout=0)


def test_close_keeps_reader_owned_stdout_open_until_reader_stops():
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

    session = cast(Any, object.__new__(RemoteSession))
    session.closed = False
    session.forwards = []
    session.output_handler = None
    session.helper_process = Process()
    session._openocd_returncode = None
    session.reader_error = None
    session._state_lock = threading.RLock()
    session._state_changed = threading.Condition(session._state_lock)
    session._terminal_reason = "process_exit"
    session.reader_thread = Reader()

    assert session.close() is None

    assert reader_stopped.is_set()
    assert not session.reader_thread.is_alive()
    assert session.helper_process.stdin.closed
    assert session.helper_process.stdout.closed
    assert session.helper_process.stderr.closed
    assert session.closed
    assert session.close() is None


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
        return later_error, []

    session._close_forwards = close_forwards
    session._close_helper = close_helper

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


def test_close_helper_retains_nested_process_cleanup_diagnostics():
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

    session = cast(Any, object.__new__(RemoteSession))
    session.helper_process = Process()
    session.reader_thread = None
    session.reader_error = None
    session.output_handler = None
    session._state_lock = threading.RLock()
    session._state_changed = threading.Condition(session._state_lock)
    session._terminal_reason = "requested"

    logical_error, cleanup_errors = session._close_helper()

    assert isinstance(logical_error, SessionError)
    assert cleanup_errors == [terminate_error]
    notes = logical_error.__notes__
    assert any("helper terminate failed" in note for note in notes)
    assert any("helper stderr close failed" in note for note in notes)
    assert all("helper cleanup also failed" in note for note in notes)


@pytest.mark.timeout(10)
def test_wait_for_openocd_exit_wakes_on_terminal_event(monkeypatch):
    waiting = threading.Event()

    class ObservableCondition(threading.Condition):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    session = cast(Any, object.__new__(RemoteSession))
    session.closed = False
    session._openocd_returncode = None
    session.reader_error = None
    session.forwards = []
    session.output_handler = None
    session._state_lock = threading.RLock()
    session._state_changed = ObservableCondition(session._state_lock)
    results = []

    monkeypatch.setattr(
        backend_module.time,
        "sleep",
        lambda *_args: (_ for _ in ()).throw(AssertionError("sleep is not event-driven")),
    )

    waiter = threading.Thread(target=lambda: results.append(session.wait_for_openocd_exit()))
    waiter.start()
    assert waiting.wait(5)
    session._dispatch({"type": "SESSION_CLOSED", "reason": "process_exit", "returncode": 0})
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert results == [0]


def test_check_openocd_exit_raises_when_ssh_forward_exits():
    class Helper:
        def poll(self):
            return None

    class Forward:
        def poll(self):
            return FORWARD_FAILURE_RC

        def stderr_tail(self):
            return b"forward failed"

    session = cast(Any, object.__new__(RemoteSession))
    session.helper_process = Helper()
    session.closed = False
    session._openocd_returncode = None
    session.reader_error = None
    session.forwards = [Forward()]
    session._state_changed = threading.Condition()

    with pytest.raises(SessionError):
        session.check_openocd_exit()


@pytest.mark.timeout(10)
def test_wait_for_openocd_exit_observes_forward_failure():
    waiting = threading.Event()
    failed = threading.Event()

    class ObservableCondition(threading.Condition):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    class Forward:
        def poll(self):
            return FORWARD_FAILURE_RC if failed.is_set() else None

        def stderr_tail(self):
            return b"forward failed"

    session = cast(Any, object.__new__(RemoteSession))
    session.closed = False
    session._openocd_returncode = None
    session.reader_error = None
    session.forwards = [Forward()]
    session._state_lock = threading.RLock()
    session._state_changed = ObservableCondition(session._state_lock)
    errors = []

    def wait_for_exit():
        try:
            session.wait_for_openocd_exit()
        except BaseException as error:
            errors.append(error)

    waiter = threading.Thread(target=wait_for_exit)
    waiter.start()
    assert waiting.wait(5)
    failed.set()
    with session._state_changed:
        session._state_changed.notify_all()
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], SessionError)


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
        monkeypatch.setattr(backend_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(backend_module.selectors, "DefaultSelector", Selector)
        deadline = 0.5
        assert not RemoteSession._await_forward_ready(
            cast(Any, process), "ZRO_FORWARD_ready", deadline
        )
    finally:
        process.stdout.close()
        os.close(write_fd)
