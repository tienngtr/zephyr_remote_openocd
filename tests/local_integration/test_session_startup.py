# SPDX-License-Identifier: Apache-2.0

"""Session publication with terminal helper events during forwarding."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import override

import pytest
from zephyr_remote_openocd.remote.backend import RemoteSession
from zephyr_remote_openocd.remote.helper_client import _HelperClient
from zephyr_remote_openocd.remote.model import RemoteProcess, RemoteSessionRequest, Service
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, SshCommand, SshLocalForward

PEER = r'''
import hashlib, json, re, sys
from pathlib import Path
root = Path(__file__).parent
command = sys.argv[-1]
def emit(kind, **fields):
    print(json.dumps(dict(version=1, type=kind, **fields)), flush=True)
if command.endswith(" control"):
    emit("SESSION_CREATED", helper="peer", session_id="session", remote_workspace="/workspace")
    request = json.loads(sys.stdin.buffer.readline())
    emit("PROCESS_STARTING", argv=request["argv"])
    emit("PROCESS_READY", remote_address="127.64.0.1", child_pid=1)
    with (root / "gate").open("rb", buffering=0) as gate:
        assert gate.read(1) == b"x"
    ending = (root / "ending").read_text()
    if ending == "error":
        emit("ERROR", code="CLEANUP_FAILED", message="failure during forwarding")
    else:
        emit("SESSION_CLOSED", reason="process_exit", returncode=int(ending))
elif " stage " in command:
    sys.stdin.buffer.read()
    emit("STAGED", byte_count=0, sha256=hashlib.sha256(b"").hexdigest(), files=[], directories=[])
elif re.search(r"ZRO_FORWARD_[0-9a-f]+", command):
    print(re.search(r"ZRO_FORWARD_[0-9a-f]+", command).group(0), flush=True)
    sys.stdin.buffer.read()
else:
    content = sys.stdin.buffer.read()
    emit("DEPLOYED", status="reused", path="/helper.py", sha256=hashlib.sha256(content).hexdigest())
'''


@pytest.mark.parametrize(
    "ending", ("0", "7", "error"), ids=("clean-exit", "failed-exit", "helper-error")
)
@pytest.mark.parametrize("auxiliary", (False, True), ids=("required-forward", "auxiliary-forward"))
def test_acquisition_rejects_helper_termination_recorded_during_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str, auxiliary: bool
):
    peer = tmp_path / "peer.py"
    peer.write_text(f"#!{sys.executable}\n" + PEER)
    peer.chmod(0o700)
    (tmp_path / "ending").write_text(ending)
    os.mkfifo(tmp_path / "gate")
    recorded = threading.Event()
    drain = _HelperClient._drain_events

    def drain_and_notify(client: _HelperClient) -> None:
        try:
            drain(client)
        finally:
            recorded.set()

    monkeypatch.setattr(_HelperClient, "_drain_events", drain_and_notify)
    transports: list[ManagedSshProcess] = []

    class Command(SshCommand):
        @override
        def popen(
            self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
        ) -> ManagedSshProcess:
            if local_forward is not None:
                with (tmp_path / "gate").open("wb", buffering=0) as gate:
                    gate.write(b"x")
                assert recorded.wait(30), "helper event reader did not record the terminal event"
            process = super().popen(host, remote_command, local_forward=local_forward)
            transports.append(process)
            return process

    service = Service("gdb", 45678, 3333)
    session = RemoteSession.prepare(
        RemoteSessionRequest(
            "controlled-host",
            Command((str(peer),)),
            RemoteProcess(("openocd",)),
            services=(service,),
            auxiliary_services=(service,) if auxiliary else (),
        )
    )
    try:
        with pytest.raises(SessionError):
            session.acquire()
        assert session.openocd_returncode == (None if ending == "error" else int(ending))
    finally:
        session.close()
    assert session.closed
    assert all(process.poll() is not None for process in transports)
    assert not session.forwarded_services
