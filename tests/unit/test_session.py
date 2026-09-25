# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading

import pytest
from zephyr_remote_openocd.remote.session import SessionError, _SessionState

OPENOCD_FAILURE_RC = 7


def test_only_natural_process_close_sets_openocd_result():
    state = _SessionState()

    state.record_close("requested", None)
    assert state.openocd_returncode is None

    state.record_close("process_exit", OPENOCD_FAILURE_RC)
    assert state.openocd_returncode == OPENOCD_FAILURE_RC


def test_unexpected_requested_close_fails_status_observation():
    state = _SessionState()

    state.record_close("requested", None)

    with pytest.raises(SessionError):
        state.recorded_openocd_exit()


@pytest.mark.timeout(10)
def test_requested_stop_serializes_close_event_with_stop_write():
    close_attempted = threading.Event()
    stop_write_entered = threading.Event()
    release_stop_write = threading.Event()
    close_recorded = threading.Event()
    observe_close = threading.Event()

    class ObservableCondition(threading.Condition):
        def __enter__(self):
            if observe_close.is_set():
                close_attempted.set()
            return super().__enter__()

    state = _SessionState()
    state._changed = ObservableCondition(state._lock)

    def write_stop():
        stop_write_entered.set()
        assert release_stop_write.wait(5)

    stopper = threading.Thread(target=lambda: state.request_stop(write_stop))
    stopper.start()
    assert stop_write_entered.wait(5)

    def record_close():
        observe_close.set()
        state.record_close("requested", None)
        close_recorded.set()

    close = threading.Thread(target=record_close)
    close.start()
    assert close_attempted.wait(5)
    assert not close_recorded.is_set()

    release_stop_write.set()
    stopper.join(timeout=5)
    close.join(timeout=5)

    assert not stopper.is_alive()
    assert not close.is_alive()
    assert close_recorded.is_set()
    assert state.recorded_openocd_exit() is None


@pytest.mark.timeout(10)
def test_session_state_wakes_on_close_event():
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
    state.record_close("process_exit", 0)
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert results == [0]
