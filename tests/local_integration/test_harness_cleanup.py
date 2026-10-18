# SPDX-License-Identifier: Apache-2.0

"""Maintained external-test cleanup paths with local owned processes."""

from __future__ import annotations

import math
import subprocess
import sys

import pytest
from zephyr_remote_openocd.controller._ssh import SshCommand, stop_process

from tests.process_support import cleanup_on_exit, read_line, stop_and_close


def test_harness_cleanup_attempts_data_pipes_after_stderr_failure(monkeypatch):
    process = SshCommand((sys.executable, "-c", "pass")).popen("controlled-host", "unused")
    failure = OSError("stderr cleanup failed")
    close_stderr = process.close_stderr

    def close_then_fail():
        close_stderr()
        raise failure

    try:
        process.wait(timeout=30)
        with monkeypatch.context() as effects:
            effects.setattr(process, "close_stderr", close_then_fail)
            with pytest.raises(OSError) as raised:
                stop_and_close(process)
        assert raised.value is failure
        assert process.stdin is not None and process.stdin.closed
        assert process.stdout is not None and process.stdout.closed
    finally:
        stop_process(process)


def test_harness_cleanup_escalation_uses_finite_waits(monkeypatch):
    code = (
        "import signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "print('ready',flush=True);signal.pause()"
    )
    process = SshCommand((sys.executable, "-c", code)).popen("controlled-host", "unused")
    wait = process.wait
    kill = process.kill
    killed = False

    def kill_process():
        nonlocal killed
        kill()
        killed = True

    def wait_process(timeout: float | None = None):
        assert timeout is not None and math.isfinite(timeout) and timeout > 0
        if not killed:
            # Model expiry at the subprocess wait boundary without real delay.
            raise subprocess.TimeoutExpired(process.args, timeout)
        return wait(timeout=timeout)

    try:
        assert process.stdout is not None
        assert read_line(process.stdout) == b"ready\n"
        with monkeypatch.context() as effects:
            effects.setattr(process, "wait", wait_process)
            effects.setattr(process, "kill", kill_process)
            stop_and_close(process)
        assert killed and process.poll() is not None
        assert process.stdin is not None and process.stdin.closed
        assert process.stdout is not None and process.stdout.closed
    finally:
        if process.poll() is None:
            kill()
            wait(timeout=30)
        stop_process(process)


@pytest.mark.parametrize("failed_pipe", ("stdin", "stderr"))
def test_process_cleanup_scope_preserves_assertion_and_closes_pipes(monkeypatch, failed_pipe):
    process = SshCommand((sys.executable, "-c", "pass")).popen("controlled-host", "unused")
    primary = AssertionError("observation failed")
    failure = OSError(f"{failed_pipe} cleanup failed")
    failure.add_note("nested pipe cleanup diagnostic")
    assert process.stdin is not None
    close_pipe = process.stdin.close if failed_pipe == "stdin" else process.close_stderr

    def close_then_fail():
        close_pipe()
        raise failure

    try:
        process.wait(timeout=30)
        with monkeypatch.context() as effects:
            if failed_pipe == "stdin":
                effects.setattr(process.stdin, "close", close_then_fail)
            else:
                effects.setattr(process, "close_stderr", close_then_fail)
            with (
                pytest.raises(AssertionError) as raised,
                cleanup_on_exit(lambda: stop_and_close(process)),
            ):
                raise primary
            assert raised.value is primary
            assert any(str(failure) in note for note in primary.__notes__)
            assert any("nested pipe cleanup diagnostic" in note for note in primary.__notes__)
            assert process.poll() is not None
            assert process.stdin is not None and process.stdin.closed
            assert process.stdout is not None and process.stdout.closed
    finally:
        stop_process(process)
