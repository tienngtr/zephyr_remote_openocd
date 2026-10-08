# SPDX-License-Identifier: Apache-2.0

"""Maintained external-test cleanup paths with local owned processes."""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, SshCommand, _stop_process

from tests.hardware import test_real_flash as flash_harness
from tests.hardware_support import FlashFixture, PreparedTarget
from tests.inventory import (
    BuildEnvironment,
    FlashOperation,
    InventoryHost,
    SerialEndpoint,
    SerialExpectation,
)
from tests.process_support import read_line
from tests.serial_reader import stop_reader
from tests.ssh_integration.test_ssh_integration import stop_and_close


@pytest.mark.parametrize("cleanup", (stop_reader, stop_and_close))
def test_harness_cleanup_attempts_data_pipes_after_stderr_failure(monkeypatch, cleanup):
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
                cleanup(process)
        assert raised.value is failure
        assert process.stdin is not None and process.stdin.closed
        assert process.stdout is not None and process.stdout.closed
    finally:
        _stop_process(process)


@pytest.mark.parametrize("cleanup", (stop_reader, stop_and_close))
def test_harness_cleanup_escalation_uses_finite_waits(monkeypatch, cleanup):
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
        assert read_line(process.stdout) == b"ready\n"
        with monkeypatch.context() as effects:
            effects.setattr(process, "wait", wait_process)
            effects.setattr(process, "kill", kill_process)
            cleanup(process)
        assert killed and process.poll() is not None
        assert process.stdin is not None and process.stdin.closed
        assert process.stdout is not None and process.stdout.closed
    finally:
        if process.poll() is None:
            kill()
            wait(timeout=30)
        _stop_process(process)


def test_flash_serial_cleanup_preserves_assertion_failure(tmp_path, monkeypatch):
    target = PreparedTarget(
        "target:profile",
        "target",
        "profile",
        InventoryHost("host", "controlled-host", ("openocd",), ("controlled-ssh",), (), ()),
        BuildEnvironment("build", tmp_path, Path("west"), ()),
        None,
        tmp_path,
        tmp_path / "config.yaml",
        (),
        (),
    )
    fixture = FlashFixture(
        target,
        FlashOperation("precondition", SerialExpectation("console", "ready", 30), (), False),
        SerialEndpoint("console", "/unused", 115200, 8, "none", 1, "none"),
        tmp_path,
    )
    raw = subprocess.Popen(
        [sys.executable, "-c", "print('{\"type\":\"ERROR\"}',flush=True)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    reader = ManagedSshProcess.from_popen(raw)
    close_stderr = reader.close_stderr
    failure = OSError("serial reader cleanup failed")

    def close_then_fail():
        close_stderr()
        raise failure

    try:
        reader.wait(timeout=30)
        with monkeypatch.context() as effects:
            effects.setattr(SshCommand, "popen", lambda *_args, **_kwargs: reader)
            effects.setattr(
                flash_harness,
                "run_process",
                lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, ""),
            )
            effects.setattr(reader, "close_stderr", close_then_fail)
            with pytest.raises(AssertionError) as raised:
                flash_harness.TestRealOpenOcdFlash().test_configured_target_flashes_and_emits_fresh_serial_output(
                    fixture
                )
        assert any(str(failure) in note for note in raised.value.__notes__)
        assert reader.stdin is not None and reader.stdin.closed
        assert reader.stdout is not None and reader.stdout.closed
    finally:
        _stop_process(reader)
