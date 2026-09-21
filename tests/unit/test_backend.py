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
    SessionAllocation,
    SessionDescriptor,
)
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import SshCommand


def test_open_acquires_complete_session_in_order(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    descriptor = SessionDescriptor(SessionAllocation("id", "/workspace"), "127.64.0.1")
    actions = []

    monkeypatch.setattr(backend_module, "deploy_helper", lambda *_args: deployment)

    def open_helper(session):
        actions.append("helper")
        session.helper_process = object()

    def start(_session, _services):
        actions.append("start")
        return descriptor

    monkeypatch.setattr(RemoteSession, "_open_helper", open_helper)
    monkeypatch.setattr(RemoteSession, "stage", lambda _session, _files: actions.append("stage"))
    monkeypatch.setattr(RemoteSession, "start", start)

    session = RemoteSession.open(request)

    assert actions == ["helper", "stage", "start"]
    assert session.descriptor is descriptor


def test_open_rolls_back_failed_acquisition_once(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    startup_error = RuntimeError("staging failed")
    actions = []

    monkeypatch.setattr(backend_module, "deploy_helper", lambda *_args: deployment)

    def open_helper(session):
        actions.append("helper")
        session.helper_process = object()

    def fail_stage(_session, _files):
        actions.append("stage")
        raise startup_error

    monkeypatch.setattr(RemoteSession, "_open_helper", open_helper)
    monkeypatch.setattr(RemoteSession, "stage", fail_stage)
    monkeypatch.setattr(RemoteSession, "close", lambda _session: actions.append("close"))

    with pytest.raises(RuntimeError) as raised:
        RemoteSession.open(request)

    assert raised.value is startup_error
    assert actions == ["helper", "stage", "close"]


@pytest.mark.timeout(10)
def test_close_disposes_streams_after_delayed_reader_stops(monkeypatch):
    release_reader = threading.Event()
    reader_started = threading.Event()
    reader_stopped = threading.Event()

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

    session = cast(Any, object.__new__(RemoteSession))
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
        reader_stopped.set()

    session.reader_thread = threading.Thread(target=consume_terminal_event)
    session.reader_thread.start()
    assert reader_started.wait(5)

    join_results: list[bool] = []

    def controlled_join(current, timeout=2.0):
        result = len(join_results) == 1
        join_results.append(result)
        if result:
            release_reader.set()
            assert reader_stopped.wait(5)
            assert current.reader_thread is not None
            current.reader_thread.join(timeout=5)
        return result

    original_stop = RemoteSession._stop_process
    stop_stream_flags = []

    def tracked_stop(process, *, close_streams=True):
        stop_stream_flags.append(close_streams)
        return original_stop(process, close_streams=close_streams)

    monkeypatch.setattr(RemoteSession, "_join_reader", controlled_join)
    monkeypatch.setattr(RemoteSession, "_stop_process", staticmethod(tracked_stop))
    try:
        session.close()
    finally:
        release_reader.set()
        assert reader_stopped.wait(5)
        session.reader_thread.join(timeout=5)

    assert join_results == [False, True]
    assert stop_stream_flags == [False, True]
    assert session.reader_thread is not None and not session.reader_thread.is_alive()
    assert session.helper_process.stdin.closed
    assert session.helper_process.stdout.closed
    assert session.helper_process.stderr.closed
    assert session.closed


@pytest.mark.timeout(10)
def test_poll_bounds_reader_join_after_helper_exit():
    class Process:
        def poll(self):
            return 0

    class Reader:
        def __init__(self):
            self.join_calls = 0
            self.join_timeout = None

        def is_alive(self):
            return True

        def join(self, timeout=None):
            self.join_calls += 1
            self.join_timeout = timeout

    session = cast(Any, object.__new__(RemoteSession))
    session.helper_process = Process()
    session.process_returncode = None
    session.reader_error = None
    session.reader_thread = Reader()
    session.forwards = []

    with pytest.raises(SessionError):
        session.poll()
    assert session.reader_thread.join_calls == 1
    assert session.reader_thread.join_timeout is not None


def test_poll_raises_when_ssh_forward_exits():
    class Helper:
        def poll(self):
            return None

    class Forward:
        def poll(self):
            return 9

        def stderr_tail(self):
            return b"forward failed"

    session = cast(Any, object.__new__(RemoteSession))
    session.helper_process = Helper()
    session.process_returncode = None
    session.reader_error = None
    session.forwards = [Forward()]

    with pytest.raises(SessionError):
        session.poll()


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
