# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote.backend import RemoteSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.helper_client import _HelperClient
from zephyr_remote_openocd.remote.model import RemoteProcess, RemoteSessionRequest, Service
from zephyr_remote_openocd.remote.ssh import SshCommand

pytestmark = pytest.mark.local


FAKE_SSH = r'''
import json
import re
import sys


def noisy_stderr(marker):
    sys.stderr.buffer.write(b"prefix-" + b"x" * 262144 + marker + b"\n")
    sys.stderr.flush()


remote_command = sys.argv[-1]
if remote_command.endswith(" control"):
    noisy_stderr(b"control-tail")
    print(json.dumps({
        "version": 1,
        "type": "SESSION_CREATED",
        "helper": "fake",
        "session_id": "session",
        "remote_workspace": "/tmp/fake-workspace",
    }), flush=True)
    for line in sys.stdin.buffer:
        message = json.loads(line)
        if message["type"] == "START":
            print(json.dumps({
                "version": 1,
                "type": "PROCESS_READY",
                "remote_address": "127.64.0.1",
                "child_pid": 1,
            }), flush=True)
        elif message["type"] == "STOP":
            print(json.dumps({
                "version": 1,
                "type": "SESSION_CLOSED",
                "reason": "requested",
                "returncode": None,
            }), flush=True)
            break
elif remote_command.startswith("python3 -c "):
    noisy_stderr(b"forward-tail")
    sentinel = re.search(r"ZRO_FORWARD_[0-9a-f]+", remote_command).group(0)
    print(sentinel, flush=True)
    sys.stdin.buffer.read()
'''


def fake_ssh_command(tmp_path: Path) -> SshCommand:
    executable = tmp_path / "fake_ssh.py"
    executable.write_text(FAKE_SSH, encoding="utf-8")
    return SshCommand((sys.executable, str(executable)))


def request(command: SshCommand, *, services=()):
    return RemoteSessionRequest(
        "fake-host",
        command,
        process=RemoteProcess((sys.executable, "-c", "pass")),
        services=tuple(services),
    )


def deployment() -> DeploymentResult:
    return DeploymentResult("/helper.py", "digest", False)


def test_control_progresses_when_configured_ssh_stderr_exceeds_pipe_capacity(tmp_path):
    backend = _opened_session(request(fake_ssh_command(tmp_path)), deployment())
    try:
        descriptor = backend._start_process(())
        assert descriptor.remote_address == "127.64.0.1"
    finally:
        backend.close()
    assert backend.closed


def test_forward_progresses_when_configured_ssh_stderr_exceeds_pipe_capacity(tmp_path):
    # The fake SSH command only exercises the readiness transport; no local
    # listener is needed for this regression.  A fixed high port also keeps the
    # test usable in restricted sandboxes where socket creation is disabled.
    local_port = 45678
    service = Service("gdb", local_port, 3333)
    backend = _opened_session(request(fake_ssh_command(tmp_path)), deployment())
    try:
        descriptor = backend._start_process((service,))
        assert descriptor.remote_address == "127.64.0.1"
    finally:
        backend.close()
    assert backend.closed


def _opened_session(request, deployment):
    session = RemoteSession(request, deployment)
    session._helper = _HelperClient.open(
        session.request.ssh_command,
        session.request.host,
        session.deployment,
    )
    return session
