# SPDX-License-Identifier: Apache-2.0

"""Helper stdout EOF revokes dependent admission before diagnostics settle."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._ssh import ManagedSshProcess, SshCommand
from zephyr_remote_openocd.controller.api import (
    SessionController,
    SessionError,
    SessionPlan,
)
from zephyr_remote_openocd.protocol.process import ProcessSpec

from tests.support import ROOT

pytestmark = [pytest.mark.local, pytest.mark.timeout(60)]

# A control peer that reports READY, then closes its only stdout while staying
# alive with stderr open until the test releases its exit gate.
PEER = ROOT / "tests/fixtures/stdout_eof_peer.py"


def _release(gate: Path) -> None:
    with gate.open("wb", buffering=0) as opened:
        opened.write(b"x")


def test_stdout_eof_revokes_admission_before_collecting_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for gate in ("eof", "exit"):
        os.mkfifo(tmp_path / gate)
    session = SessionController(
        SessionPlan(
            "local-test",
            SshCommand((sys.executable, str(PEER), str(tmp_path))),
            ProcessSpec(("child",)),
            (),
        ),
        DeploymentResult("/helper.py", "digest", False),
    )
    entered = threading.Event()
    released = threading.Event()
    stderr_tail = ManagedSshProcess.stderr_tail

    def stalled_tail(process: ManagedSshProcess) -> bytes:
        # The first diagnostic collection is the reader's response to EOF.
        if not entered.is_set():
            entered.set()
            assert released.wait(30), "stderr diagnostics were not released"
        return stderr_tail(process)

    monkeypatch.setattr(ManagedSshProcess, "stderr_tail", stalled_tail)
    launched: list[bool] = []
    try:
        session.acquire()
        _release(tmp_path / "eof")
        assert entered.wait(30), "reader did not collect EOF diagnostics"
        with pytest.raises(SessionError):
            session.run_local_client(lambda: launched.append(True))
        # The refusal waits for the reader's failure rather than replacing it.
        assert session._authority.snapshot().failure is None
    finally:
        released.set()
        if entered.is_set():
            _release(tmp_path / "exit")
        with pytest.raises(SessionError) as finished:
            session.finish()
    assert not launched
    assert "remote helper terminated" in str(finished.value)
