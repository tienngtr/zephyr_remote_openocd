# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading

import pytest
from zephyr_remote_openocd.remote.session import SessionError, _SessionState

OPENOCD_FAILURE_RC = 7


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
