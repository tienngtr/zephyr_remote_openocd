# SPDX-License-Identifier: Apache-2.0

"""Controller-lease classification through a real helper and SSH stand-in."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller.api import SessionController, SessionPlan, SshCommand
from zephyr_remote_openocd.controller.authority import SessionError
from zephyr_remote_openocd.protocol.process import ProcessSpec

from tests.process_support import cleanup_on_exit
from tests.support import ROOT

pytestmark = [pytest.mark.local, pytest.mark.skipif(os.name != "posix", reason="POSIX FIFOs")]

RELAY = ROOT / "tests/fixtures/remote_eof_ssh.py"


@pytest.mark.timeout(60)
@pytest.mark.parametrize("remote_only_eof", (False, True), ids=("released", "remote-only"))
def test_remote_only_controller_eof_fails_although_terminal_arrives_after_local_closure(
    tmp_path: Path, remote_only_eof: bool
) -> None:
    for directory in ("home", "runtime"):
        (tmp_path / directory).mkdir()
    os.mkfifo(tmp_path / "inject")
    os.mkfifo(tmp_path / "injected")
    session = SessionController.prepare(
        SessionPlan(
            "test-host",
            SshCommand((sys.executable, str(RELAY), str(tmp_path))),
            ProcessSpec(
                (sys.executable, str(ROOT / "tests/fixtures/idle_child.py"), "ZRO_TEST_READY"),
                required_output_sentinels=("ZRO_TEST_READY",),
            ),
        )
    )
    with cleanup_on_exit(session.close):
        session.acquire()
        if not remote_only_eof:
            session.finish()
            return
        # The relay ends only remote input while the local operation still owns
        # the lease, then withholds SESSION_ENDED until local closure begins.
        with (tmp_path / "inject").open("wb", buffering=0) as inject:
            inject.write(b"E")
        with (tmp_path / "injected").open("rb", buffering=0) as injected:
            assert injected.read(1) == b"E"
        with pytest.raises(SessionError, match="CONTROLLER_LEASE_LOST"):
            session.finish()
