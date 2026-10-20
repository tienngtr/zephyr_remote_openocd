# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._ssh import (
    SSH_STDERR_TAIL_BYTES,
    ManagedSshProcess,
    ProcessAcquiredHandler,
    SshCommand,
    SshForwardSpec,
    stop_process,
)
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionController,
    SessionPlan,
)
from zephyr_remote_openocd.protocol.process import ProcessSpec

from tests.support import fixture_command

pytestmark = pytest.mark.local


def fake_ssh_command(tmp_path: Path) -> SshCommand:
    return SshCommand((str(fixture_command(tmp_path, "noisy_ssh.py")),))


def request(command: SshCommand, *, forwards: Iterable[ForwardSpec] = ()) -> SessionPlan:
    return SessionPlan(
        "fake-host",
        command,
        process=ProcessSpec((sys.executable, "-c", "pass")),
        forwards=tuple(forwards),
    )


def deployment() -> DeploymentResult:
    return DeploymentResult("/helper.py", "digest", False)


def test_control_progresses_when_configured_ssh_stderr_exceeds_pipe_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transports: list[ManagedSshProcess] = []
    popen = SshCommand.popen

    def record_transport(
        command: SshCommand,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired: ProcessAcquiredHandler | None = None,
    ) -> ManagedSshProcess:
        transport = popen(
            command, host, remote_command, local_forward=local_forward, on_acquired=on_acquired
        )
        transports.append(transport)
        return transport

    monkeypatch.setattr(SshCommand, "popen", record_transport)
    session_controller = SessionController(request(fake_ssh_command(tmp_path)), deployment())
    try:
        session_controller.acquire()
        assert session_controller.descriptor is not None
        assert session_controller.descriptor.remote_address == "127.64.0.1"
    finally:
        session_controller.close()
    assert session_controller.closed
    assert len(transports) == 1
    control = transports[0]
    assert control.poll() == 0
    tail = control.stderr_tail()
    assert len(tail) <= SSH_STDERR_TAIL_BYTES
    assert tail.endswith(b"control-tail\n")


def test_forward_progresses_when_configured_ssh_stderr_exceeds_pipe_capacity(
    tmp_path: Path,
) -> None:
    # The fake SSH endpoint exercises forwarding readiness and stderr draining;
    # the readiness sentinel does not require an actual forwarded listener.
    local_port = 45678
    forward = ForwardSpec("gdb", local_port, 3333)
    session_controller = SessionController(
        request(fake_ssh_command(tmp_path), forwards=(forward,)), deployment()
    )
    try:
        session_controller.acquire()
        assert session_controller.descriptor is not None
        assert session_controller.descriptor.remote_address == "127.64.0.1"
        assert session_controller.forwarded_specs == (forward,)
    finally:
        session_controller.close()
    assert session_controller.closed


@pytest.mark.timeout(60)
def test_stop_releases_stderr_held_open_outside_the_transport() -> None:
    """A connection-sharing master holds passed client stdio until its channel closes."""
    transport = SshCommand((sys.executable, "-c", "import sys; sys.stdin.buffer.read()")).popen(
        "fake-host", "serve"
    )
    # A writer the runner does not own, like the master's copy of client stderr.
    holder = os.open(f"/proc/{transport.pid}/fd/2", os.O_WRONLY | os.O_CLOEXEC)
    try:
        stop_process(transport)
        assert transport.returncode is not None
        # No local reader remains on the pipe once cleanup has returned.
        with pytest.raises(BrokenPipeError):
            os.write(holder, b"late diagnostic")
    finally:
        os.close(holder)
        stop_process(transport)
