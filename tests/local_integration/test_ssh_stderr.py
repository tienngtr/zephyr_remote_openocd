# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller.deployment import DeploymentResult
from zephyr_remote_openocd.controller.model import Service, SessionPlan
from zephyr_remote_openocd.controller.session import SessionController
from zephyr_remote_openocd.controller.ssh import (
    SSH_STDERR_TAIL_BYTES,
    ManagedSshProcess,
    SshCommand,
    SshForwardSpec,
)
from zephyr_remote_openocd.protocol.process import ProcessSpec

pytestmark = pytest.mark.local


FAKE_SSH = r'''
import hashlib
import io
import json
import re
import shlex
import sys
import tarfile


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
                "type": "ATTEMPT", "argv": ["test-process"],
            }), flush=True)
            print(json.dumps({
                "version": 1,
                "type": "READY", "remote_address": "127.64.0.1",
                "child_pid": 1,
            }), flush=True)
    print(json.dumps(dict(version=1, type="SESSION_ENDED", trigger="controller_eof",
        primary_failure=None, diagnostics=[], child_result=None,
        cleanup=dict(child_disposal="confirmed", workspace_disposal="confirmed",
                     residual_resources=[]))), flush=True)
elif shlex.split(remote_command)[2] == "stage":
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as archive:
        assert archive.getmembers() == []
    print(json.dumps(dict(version=1, type="STAGED", files=[], directories=[],
                         byte_count=0, sha256=hashlib.sha256(b"").hexdigest())), flush=True)
elif remote_command.startswith("python3 -c "):
    noisy_stderr(b"forward-tail")
    sentinel = re.search(r"ZRO_FORWARD_[0-9a-f]+", remote_command).group(0)
    print(sentinel, flush=True)
    sys.stdin.buffer.read()
'''


def fake_ssh_command(tmp_path: Path) -> SshCommand:
    executable = tmp_path / "fake_ssh.py"
    executable.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(FAKE_SSH)} \"$@\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return SshCommand((str(executable),))


def request(command: SshCommand, *, services=()):
    return SessionPlan(
        "fake-host",
        command,
        process=ProcessSpec((sys.executable, "-c", "pass")),
        services=tuple(services),
    )


def deployment() -> DeploymentResult:
    return DeploymentResult("/helper.py", "digest", False)


def test_control_progresses_when_configured_ssh_stderr_exceeds_pipe_capacity(tmp_path, monkeypatch):
    transports: list[ManagedSshProcess] = []
    popen = SshCommand.popen

    def record_transport(
        command: SshCommand,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
    ) -> ManagedSshProcess:
        transport = popen(command, host, remote_command, local_forward=local_forward)
        transports.append(transport)
        return transport

    monkeypatch.setattr(SshCommand, "popen", record_transport)
    backend = SessionController(request(fake_ssh_command(tmp_path)), deployment())
    try:
        backend.acquire()
        assert backend.descriptor is not None
        assert backend.descriptor.remote_address == "127.64.0.1"
    finally:
        backend.close()
    assert backend.closed
    assert len(transports) == 1
    control = transports[0]
    assert control.poll() == 0
    tail = control.stderr_tail()
    assert len(tail) <= SSH_STDERR_TAIL_BYTES
    assert tail.endswith(b"control-tail\n")


def test_forward_progresses_when_configured_ssh_stderr_exceeds_pipe_capacity(tmp_path):
    # The fake SSH endpoint exercises forwarding readiness and stderr draining;
    # the readiness sentinel does not require an actual forwarded listener.
    local_port = 45678
    service = Service("gdb", local_port, 3333)
    backend = SessionController(
        request(fake_ssh_command(tmp_path), services=(service,)), deployment()
    )
    try:
        backend.acquire()
        assert backend.descriptor is not None
        assert backend.descriptor.remote_address == "127.64.0.1"
        assert backend.forwarded_services == (service,)
    finally:
        backend.close()
    assert backend.closed
