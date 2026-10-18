# SPDX-License-Identifier: Apache-2.0

"""Session publication with terminal helper events during forwarding."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import override

import pytest
from zephyr_remote_openocd.controller import _forwarding as forwarding_module
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    ProcessAcquiredHandler,
    SshCommand,
    SshForwardSpec,
)
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionController,
    SessionError,
    SessionPlan,
)
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import ProtocolError

from tests.process_support import cleanup_on_exit
from tests.support import fixture_command


@pytest.mark.parametrize(
    "terminal", ("0", "7", "error"), ids=("clean-exit", "failed-exit", "helper-error")
)
@pytest.mark.parametrize("auxiliary", (False, True), ids=("required-forward", "auxiliary-forward"))
def test_acquisition_rejects_helper_termination_recorded_during_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str, auxiliary: bool
) -> None:
    peer = fixture_command(tmp_path, "terminal_during_forward_peer.py", str(tmp_path))
    (tmp_path / "terminal").write_text(terminal)
    os.mkfifo(tmp_path / "gate")
    recorded = threading.Event()
    drain = HelperConnection._drain_events

    def drain_and_notify(client: HelperConnection) -> None:
        try:
            drain(client)
        finally:
            recorded.set()

    monkeypatch.setattr(HelperConnection, "_drain_events", drain_and_notify)
    transports: list[ManagedSshProcess] = []

    class Command(SshCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired: ProcessAcquiredHandler | None = None,
        ) -> ManagedSshProcess:
            if local_forward is not None:
                with (tmp_path / "gate").open("wb", buffering=0) as gate:
                    gate.write(b"x")
                assert recorded.wait(30), "helper event reader did not record the terminal event"
            process = super().popen(
                host, remote_command, local_forward=local_forward, on_acquired=on_acquired
            )
            transports.append(process)
            return process

    forward = ForwardSpec("gdb", 45678, 3333)
    session = SessionController.prepare(
        SessionPlan(
            "controlled-host",
            Command((str(peer),)),
            ProcessSpec(("openocd",)),
            forwards=(forward,),
            auxiliary_forwards=(forward,) if auxiliary else (),
        )
    )
    try:
        with pytest.raises(SessionError):
            session.acquire()
        assert session.openocd_returncode == (None if terminal == "error" else int(terminal))
    finally:
        session.close()
    assert session.closed
    assert all(process.poll() is not None for process in transports)
    assert not session.forwarded_specs


@pytest.mark.parametrize("terminal", ("7", "error", "reader"))
@pytest.mark.parametrize("required", (True, False), ids=("required", "best-effort"))
def test_failure_recorded_during_forward_readiness_remains_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str, required: bool
) -> None:
    peer = fixture_command(tmp_path, "terminal_during_forward_peer.py", str(tmp_path))
    (tmp_path / "terminal").write_text(terminal)
    os.mkfifo(tmp_path / "gate")
    recorded = threading.Event()
    drain = HelperConnection._drain_events

    def drain_and_notify(client: HelperConnection) -> None:
        try:
            drain(client)
        finally:
            recorded.set()

    monkeypatch.setattr(HelperConnection, "_drain_events", drain_and_notify)
    transports: list[ManagedSshProcess] = []

    def fail_readiness(process: ManagedSshProcess, _sentinel: str, _deadline: float) -> bool:
        transports.append(process)
        with (tmp_path / "gate").open("wb", buffering=0) as gate:
            gate.write(b"x")
        assert recorded.wait(30), "helper reader did not record the session failure"
        error = OSError("later forward readiness failure")
        error.add_note("forward diagnostic detail")
        raise error

    monkeypatch.setattr(forwarding_module, "await_ready", fail_readiness)
    session = SessionController.prepare(
        SessionPlan("controlled-host", SshCommand((str(peer),)), ProcessSpec(("openocd",)))
    )
    try:
        session.acquire()
        with pytest.raises(SessionError) as raised:
            session.forward((ForwardSpec("rtt", 45678, 5555),), required=required)
        if terminal == "reader":
            assert "READER_FAILURE" in str(raised.value)
            assert isinstance(raised.value.__cause__, ProtocolError)
        elif terminal == "error":
            assert "CLEANUP_FAILED" in str(raised.value)
        else:
            assert "7" in str(raised.value)
        assert "later forward readiness failure" not in str(raised.value)
        notes = "\n".join(raised.value.__notes__)
        assert "later forward readiness failure" in notes
        assert "forward diagnostic detail" in notes
        assert not session.forwarded_specs
        assert transports and all(process.poll() is not None for process in transports)
    finally:
        if terminal == "reader":
            with pytest.raises(SessionError):
                session.close()
        else:
            session.close()
    assert session.closed


@pytest.mark.parametrize("terminal", ("0", "7", "error"))
@pytest.mark.parametrize("required", (True, False), ids=("required", "best-effort"))
def test_recorded_terminal_prevents_new_forward_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str, required: bool
) -> None:
    peer = fixture_command(tmp_path, "terminal_during_forward_peer.py", str(tmp_path))
    (tmp_path / "terminal").write_text(terminal)
    os.mkfifo(tmp_path / "gate")
    recorded = threading.Event()
    drain = HelperConnection._drain_events

    def drain_and_notify(client: HelperConnection) -> None:
        try:
            drain(client)
        finally:
            recorded.set()

    monkeypatch.setattr(HelperConnection, "_drain_events", drain_and_notify)
    forward_entries: list[SshForwardSpec] = []

    class Command(SshCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired: ProcessAcquiredHandler | None = None,
        ) -> ManagedSshProcess:
            if local_forward is not None:
                forward_entries.append(local_forward)
                raise OSError("later forward creation failure")
            return super().popen(host, remote_command, on_acquired=on_acquired)

    session = SessionController.prepare(
        SessionPlan("controlled-host", Command((str(peer),)), ProcessSpec(("openocd",)))
    )
    with cleanup_on_exit(session.close):
        session.acquire()
        with (tmp_path / "gate").open("wb", buffering=0) as gate:
            gate.write(b"x")
        assert recorded.wait(30), "helper reader did not record the terminal event"
        with pytest.raises(SessionError) as raised:
            session.forward((ForwardSpec("rtt", 45678, 5555),), required=required)
        assert not forward_entries
        assert not session.forwarded_specs
        assert "later forward creation failure" not in str(raised.value)
        if terminal == "error":
            assert "failure during forwarding" in str(raised.value)
        elif terminal == "7":
            assert "7" in str(raised.value)
    assert session.closed
