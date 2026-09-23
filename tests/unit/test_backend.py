# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from typing import Any, cast

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote.backend import RemoteSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.helper_client import _HelperClient, _HelperCloseResult
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


def test_wait_for_openocd_exit_raises_helper_timeout_at_deadline(monkeypatch):
    requested_timeout = 2.5
    clock = [0.0]

    class Helper:
        openocd_returncode = None

        def __init__(self):
            self.wait_timeouts = []
            self.expired_timeout = None
            self.timeout_error = subprocess.TimeoutExpired(("python3", "helper"), requested_timeout)

        @staticmethod
        def recorded_openocd_exit():
            return None

        def wait_for_change(self, timeout):
            self.wait_timeouts.append(timeout)
            assert timeout is not None and timeout > 0
            clock[0] += timeout

        def timeout_expired(self, timeout):
            self.expired_timeout = timeout
            return self.timeout_error

    class Forwards:
        has_forwards = False

        @staticmethod
        def check_health():
            pass

    helper = Helper()
    session = cast(Any, object.__new__(RemoteSession))
    session.closed = False
    session._helper = helper
    session._forwards = Forwards()
    monkeypatch.setattr(backend_module.time, "monotonic", lambda: clock[0])

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        session.wait_for_openocd_exit(requested_timeout)

    assert raised.value is helper.timeout_error
    assert helper.expired_timeout == requested_timeout
    assert clock[0] == requested_timeout
    assert all(0 < wait <= requested_timeout for wait in helper.wait_timeouts)
