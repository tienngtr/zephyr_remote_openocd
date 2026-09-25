# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading

import pytest
from zephyr_remote_openocd.remote.session import (
    SessionError,
    _HelperError,
    _SessionClosed,
    _SessionObservations,
    _StopWritten,
)

OPENOCD_FAILURE_RC = 7


def test_request_stop_reports_that_stop_was_written():
    observations = _SessionObservations()
    stop_written = threading.Event()

    result = observations.request_stop(stop_written.set)

    assert result == _StopWritten()
    assert stop_written.is_set()
    assert observations.snapshot().stop_requested


def test_request_stop_returns_existing_session_close_without_writing_stop():
    observations = _SessionObservations()
    observations.record_close("process_exit", OPENOCD_FAILURE_RC)
    stop_written = threading.Event()

    result = observations.request_stop(stop_written.set)

    assert result == _SessionClosed("process_exit", OPENOCD_FAILURE_RC)
    assert not stop_written.is_set()


def test_request_stop_returns_existing_helper_error_without_writing_stop():
    observations = _SessionObservations()
    error = SessionError("background failed")
    observations.record_error_event(error)
    stop_written = threading.Event()

    result = observations.request_stop(stop_written.set)

    assert result == _HelperError(error)
    assert not stop_written.is_set()


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

    observations = _SessionObservations()
    observations._changed = ObservableCondition(observations._lock)
    stop_results = []

    def write_stop():
        stop_write_entered.set()
        assert release_stop_write.wait(5)

    stopper = threading.Thread(
        target=lambda: stop_results.append(observations.request_stop(write_stop))
    )
    stopper.start()
    assert stop_write_entered.wait(5)

    def record_close():
        observe_close.set()
        observations.record_close("requested", None)
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
    assert stop_results == [_StopWritten()]
    snapshot = observations.snapshot()
    assert snapshot.ending == _SessionClosed("requested", None)
    assert snapshot.reader_failure is None


@pytest.mark.timeout(10)
def test_session_observations_wakes_on_process_exit():
    waiting = threading.Event()
    results = []

    class ObservableCondition(threading.Condition):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    observations = _SessionObservations()
    observations._changed = ObservableCondition(observations._lock)

    def wait_for_result():
        observations.wait_for_change(None)
        results.append(observations.snapshot())

    waiter = threading.Thread(target=wait_for_result)
    waiter.start()
    assert waiting.wait(5)
    observations.record_close("process_exit", 0)
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert results == [observations.snapshot()]
    assert results[0].ending == _SessionClosed("process_exit", 0)


def test_reader_failure_remains_independent_of_session_ending():
    observations = _SessionObservations()
    reader_error = RuntimeError("protocol failed")

    observations.record_close("process_exit", 0)
    observations.record_reader_failure(reader_error)

    snapshot = observations.snapshot()
    assert snapshot.ending == _SessionClosed("process_exit", 0)
    assert snapshot.reader_failure is reader_error
