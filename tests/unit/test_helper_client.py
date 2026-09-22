# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import subprocess
import threading
from typing import Any, cast

import pytest
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.helper_client import _HelperClient
from zephyr_remote_openocd.remote.protocol import encode_message
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import SshCommand

OPENOCD_FAILURE_RC = 7


def _helper_client() -> _HelperClient:
    return _HelperClient(SshCommand(), "host", DeploymentResult("/helper.py", "digest", False))


@pytest.mark.timeout(10)
def test_recorded_openocd_exit_is_available_while_reader_remains_alive():
    helper_client = _helper_client()
    terminal_recorded = threading.Event()
    release_reader = threading.Event()

    def consume_terminal() -> None:
        helper_client._dispatch(
            {
                "type": "SESSION_CLOSED",
                "reason": "process_exit",
                "returncode": OPENOCD_FAILURE_RC,
            }
        )
        terminal_recorded.set()
        release_reader.wait()

    reader = threading.Thread(target=consume_terminal)
    reader.start()
    assert terminal_recorded.wait(5)
    try:
        assert reader.is_alive()
        assert helper_client.recorded_openocd_exit() == OPENOCD_FAILURE_RC
    finally:
        release_reader.set()
        reader.join(timeout=5)

    assert not reader.is_alive()


def test_reader_failure_takes_precedence_over_known_openocd_result():
    helper_client = _helper_client()
    helper_client._state.record_terminal("process_exit", OPENOCD_FAILURE_RC)
    reader_error = RuntimeError("protocol failed")
    helper_client._state.record_reader_failure(reader_error)

    with pytest.raises(SessionError) as raised:
        helper_client.recorded_openocd_exit()

    assert raised.value.__cause__ is reader_error


def test_reader_failure_is_reported_when_helper_exits_without_terminal_result():
    helper_client = _helper_client()
    reader_error = SessionError("remote helper exited without a terminal event")
    helper_client._state.record_reader_failure(reader_error)

    with pytest.raises(SessionError) as raised:
        helper_client.recorded_openocd_exit()

    assert raised.value.__cause__ is reader_error


def test_close_keeps_stop_failure_primary_when_forced_disposal_also_fails():
    graceful_stop_error = RuntimeError("graceful stop failed")
    forced_stop_error = RuntimeError("forced stop failed")

    class FailingStdin:
        closed = False

        def write(self, _payload):
            raise graceful_stop_error

        @staticmethod
        def flush():
            pass

        def close(self):
            self.closed = True

    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.stdin = FailingStdin()
            self.stdout = io.BytesIO(
                encode_message("SESSION_CLOSED", reason="requested", returncode=None)
            )
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            raise forced_stop_error

        def kill(self):
            self.returncode = -9

        @staticmethod
        def stderr_tail():
            return b""

        @staticmethod
        def close_stderr():
            pass

    helper_client = _helper_client()
    cast(Any, helper_client)._process = Process()

    result = helper_client.close()

    assert result.error is graceful_stop_error
    assert result.cleanup_errors == (forced_stop_error,)
    assert any("helper cleanup also failed" in note for note in graceful_stop_error.__notes__)


def test_close_reports_helper_stop_timeout():
    class Process:
        def __init__(self):
            self.args = ("fake-helper",)
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(
                encode_message("SESSION_CLOSED", reason="requested", returncode=None)
            )
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if timeout is not None and self.returncode is None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        @staticmethod
        def stderr_tail():
            return b""

        @staticmethod
        def close_stderr():
            pass

    helper_client = _helper_client()
    cast(Any, helper_client)._process = Process()

    result = helper_client.close()

    assert isinstance(result.error, subprocess.TimeoutExpired)
