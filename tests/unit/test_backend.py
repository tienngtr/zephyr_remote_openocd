# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import threading

import pytest
from zephyr_remote_openocd.remote.backend import SshHelperSession


@pytest.mark.timeout(10)
def test_close_disposes_streams_after_delayed_reader_stops(monkeypatch):
    release_reader = threading.Event()
    reader_started = threading.Event()

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.returncode = 0
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def poll(self):
            return self.returncode

        def terminate(self):
            raise AssertionError("dead helper should not be terminated")

        def kill(self):
            raise AssertionError("dead helper should not be killed")

        def wait(self, timeout=None):
            return self.returncode

        def close_stderr(self):
            self.stderr.close()

    session = object.__new__(SshHelperSession)
    session.closed = False
    session.forwards = []
    session.output_handler = None
    session.helper_process = Process()
    session.process_returncode = None
    session.reader_error = None
    session._state_lock = threading.RLock()
    session._terminal_reason = None

    def consume_terminal_event():
        reader_started.set()
        release_reader.wait()
        with session._state_lock:
            session._terminal_reason = "requested"

    session.reader_thread = threading.Thread(target=consume_terminal_event)
    session.reader_thread.start()
    reader_started.wait()

    original_join = SshHelperSession._join_reader
    join_results = []
    join_calls = 0

    def bounded_join(current, timeout=2.0):
        nonlocal join_calls
        join_calls += 1
        if join_calls == 2:
            release_reader.set()
        result = original_join(current, timeout=0.05)
        join_results.append(result)
        return result

    original_stop = SshHelperSession._stop_process
    stop_stream_flags = []

    def tracked_stop(process, *, close_streams=True):
        stop_stream_flags.append(close_streams)
        return original_stop(process, close_streams=close_streams)

    monkeypatch.setattr(SshHelperSession, "_join_reader", bounded_join)
    monkeypatch.setattr(SshHelperSession, "_stop_process", staticmethod(tracked_stop))
    try:
        session.close()
    finally:
        release_reader.set()
        session.reader_thread.join()

    assert join_results == [False, True]
    assert stop_stream_flags == [False, True]
    assert session.reader_thread is not None and not session.reader_thread.is_alive()
    assert session.helper_process.stdin.closed
    assert session.helper_process.stdout.closed
    assert session.helper_process.stderr.closed
    assert session.closed
