# SPDX-License-Identifier: Apache-2.0

"""Fault injection at SSH harness acquisition and cleanup boundaries."""

from __future__ import annotations

import io
from unittest.mock import create_autospec

import pytest
from zephyr_remote_openocd.remote.backend import RemoteSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    SessionAllocation,
    SessionDescriptor,
)
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, SshCommand

from tests.ssh_integration import test_ssh_integration as harness_module


@pytest.mark.parametrize(
    "failure", ("second-open", "second-close", "both-close", "assertion-and-close")
)
def test_concurrent_ssh_harness_closes_every_acquired_session(monkeypatch, failure):
    ssh = SshCommand(("controlled-ssh",))
    request = RemoteSessionRequest("controlled-host", ssh, RemoteProcess(("echo",)))
    sessions = [
        RemoteSession(request, DeploymentResult("/helper.py", "digest", False)) for _ in range(2)
    ]
    for index, session in enumerate(sessions):
        session.descriptor = SessionDescriptor(
            SessionAllocation(str(index), f"/workspace/{index}"), f"127.64.0.{index + 1}"
        )
    second_error = RuntimeError("second resource failed")
    first_error = RuntimeError("first cleanup failed")
    if failure == "assertion-and-close":
        sessions[1].descriptor = sessions[0].descriptor
    opened: list[RemoteSession] = []
    close = RemoteSession.close

    def open_session(_request):
        if opened and failure == "second-open":
            raise second_error
        session = sessions[len(opened)]
        opened.append(session)
        return session

    def close_session(session):
        close(session)
        if session is sessions[1]:
            raise second_error
        if failure == "both-close":
            raise first_error

    monkeypatch.setattr(RemoteSession, "open", open_session)
    monkeypatch.setattr(RemoteSession, "close", close_session)
    ports = iter((12345, 12346))
    monkeypatch.setattr(harness_module, "free_loopback_port", lambda: next(ports))
    test = harness_module.TestSshTransportIntegration()
    test.host = request.host
    test.ssh = ssh
    with pytest.raises((RuntimeError, AssertionError)) as raised:
        test.test_concurrent_sessions_isolate_identical_remote_ports()
    assert opened
    assert all(session.closed for session in opened)
    if failure == "assertion-and-close":
        assert isinstance(raised.value, AssertionError)
        assert any(str(second_error) in note for note in raised.value.__notes__)
    else:
        assert raised.value is second_error
    if failure == "both-close":
        assert any(str(first_error) in note for note in raised.value.__notes__)


@pytest.mark.parametrize(
    "failure", ("tunnel-open", "helper-close", "tunnel-close", "both-close", "assertion-and-close")
)
def test_configured_ssh_harness_preserves_failure_and_closes_transports(monkeypatch, failure):
    helper = create_autospec(ManagedSshProcess, instance=True)
    tunnel = create_autospec(ManagedSshProcess, instance=True)
    for process in (helper, tunnel):
        process.stdout = io.BytesIO(b"3333\n")
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        process.wait.return_value = 0
    cleanup_error = RuntimeError("helper cleanup failed")
    tunnel_error = RuntimeError("tunnel cleanup failed")
    primary = AssertionError("forwarded echo failed")
    if failure in {"helper-close", "both-close", "assertion-and-close"}:
        helper.close_stderr.side_effect = cleanup_error
    if failure in {"tunnel-close", "both-close", "assertion-and-close"}:
        tunnel.close_stderr.side_effect = tunnel_error
    ssh = create_autospec(SshCommand, instance=True)
    ssh.popen.side_effect = [helper, primary if failure == "tunnel-open" else tunnel]
    monkeypatch.setattr(harness_module, "read_line", lambda _stream: b"3333\n")
    monkeypatch.setattr(harness_module, "free_loopback_port", lambda: 12345)
    monkeypatch.setattr(harness_module._ForwardManager, "_await_ready", lambda *_args: True)

    def echo(_port, payload, _timeout):
        if failure == "assertion-and-close":
            raise primary
        if payload == b"must_not_echo":
            raise AssertionError("remote echo has stopped")

    monkeypatch.setattr(harness_module, "wait_for_echo", echo)
    test = harness_module.TestSshTransportIntegration()
    test.host = "controlled-host"
    test.ssh = ssh
    with pytest.raises((RuntimeError, AssertionError)) as raised:
        test.test_forwarding_and_session_lifecycle_use_configured_client()
    helper.terminate.assert_called_once()
    helper.close_stderr.assert_called_once()
    if failure != "tunnel-open":
        tunnel.terminate.assert_called_once()
        tunnel.close_stderr.assert_called_once()
    if failure == "helper-close":
        assert raised.value is cleanup_error
    elif failure in {"tunnel-close", "both-close"}:
        assert raised.value is tunnel_error
    else:
        assert raised.value is primary
    if failure in {"both-close", "assertion-and-close"}:
        assert any(str(cleanup_error) in note for note in raised.value.__notes__)
    acquired = (helper,) if failure == "tunnel-open" else (helper, tunnel)
    assert all(process.stdin.closed and process.stdout.closed for process in acquired)


def test_external_master_input_close_failure_does_not_skip_process_cleanup(monkeypatch, tmp_path):
    primary = AssertionError("master readiness failed")
    cleanup_failure = OSError("master input close failed")

    class FailingInput(io.BytesIO):
        def close(self) -> None:
            super().close()
            raise cleanup_failure

    master = create_autospec(ManagedSshProcess, instance=True)
    master.stdin = FailingInput()
    master.stdout = io.BytesIO()
    master.poll.return_value = None
    master.wait.return_value = 0
    monkeypatch.setattr(SshCommand, "popen", lambda *_args, **_kwargs: master)
    monkeypatch.setattr(harness_module, "free_loopback_port", lambda: 12345)

    def read(_stream):
        raise primary

    monkeypatch.setattr(harness_module, "read_line", read)
    test = harness_module.TestSshTransportIntegration()
    test.host = "controlled-host"
    test.ssh = SshCommand(("controlled-ssh",))
    with pytest.raises(AssertionError) as raised:
        test.test_preferred_address_reuses_forward_retained_by_external_master(tmp_path)
    assert raised.value is primary
    assert master.terminate.called and master.close_stderr.called
    assert master.stdin.closed and master.stdout.closed
    assert any(str(cleanup_failure) in note for note in raised.value.__notes__)
