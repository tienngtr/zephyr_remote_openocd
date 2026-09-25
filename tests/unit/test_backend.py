# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from typing import override

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote.backend import RemoteSession, query_remote_openocd_version
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.helper_client import _HelperClient, _HelperCloseResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
)
from zephyr_remote_openocd.remote.protocol import encode_message
from zephyr_remote_openocd.remote.session import SessionClosedError, SessionError
from zephyr_remote_openocd.remote.ssh import SshCommand

OPENOCD_FAILURE_RC = 7
FORWARD_FAILURE_RC = 13
HELPER_FAILURE_RC = 17


class _BlockedHelper:
    @property
    def openocd_returncode(self) -> int | None:
        raise AssertionError("openocd_returncode is not expected")

    @property
    def allocation(self) -> SessionAllocation:
        raise AssertionError("allocation is not expected")

    def start_process(self, process: RemoteProcess, services: Iterable[Service]) -> str:
        del process, services
        raise AssertionError("start_process() is not expected")

    def recorded_openocd_exit(self) -> int | None:
        raise AssertionError("recorded_openocd_exit() is not expected")

    def wait_for_change(self, timeout: float | None) -> None:
        del timeout
        raise AssertionError("wait_for_change() is not expected")

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        del timeout
        raise AssertionError("timeout_expired() is not expected")

    def close(self) -> _HelperCloseResult:
        raise AssertionError("close() is not expected")


class _BlockedForwards:
    @property
    def has_forwards(self) -> bool:
        raise AssertionError("has_forwards is not expected")

    def start(self, services: Iterable[Service], remote_address: str) -> None:
        del services, remote_address
        raise AssertionError("start() is not expected")

    def check_health(self) -> None:
        raise AssertionError("check_health() is not expected")

    def close(self) -> None:
        raise AssertionError("close() is not expected")


def _make_session() -> RemoteSession:
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    return RemoteSession(request, deployment)


def test_open_rolls_back_failed_acquisition_once(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    startup_error = RuntimeError("staging failed")
    cleanup_calls = 0

    def deploy(_ssh_command, _host):
        return deployment

    def open_helper(_ssh_command, _host, _deployment, *, output_handler=None):
        return _BlockedHelper()

    monkeypatch.setattr(backend_module, "deploy_helper", deploy)

    def fail_stage(_session, _files):
        raise startup_error

    def close(_session):
        nonlocal cleanup_calls
        cleanup_calls += 1

    monkeypatch.setattr(_HelperClient, "open", open_helper)
    monkeypatch.setattr(RemoteSession, "_stage", fail_stage)
    monkeypatch.setattr(RemoteSession, "close", close)

    with pytest.raises(RuntimeError) as raised:
        RemoteSession.open(request)

    assert raised.value is startup_error
    assert cleanup_calls == 1


def test_open_retains_nested_rollback_cleanup_diagnostics(monkeypatch):
    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    startup_error = RuntimeError("staging failed")
    cleanup_error = RuntimeError("session cleanup failed")
    cleanup_error.add_note("additional cleanup failure: forward cleanup failed")

    def deploy(_ssh_command, _host):
        return deployment

    def open_helper(_ssh_command, _host, _deployment, *, output_handler=None):
        return _BlockedHelper()

    monkeypatch.setattr(backend_module, "deploy_helper", deploy)
    monkeypatch.setattr(_HelperClient, "open", open_helper)
    monkeypatch.setattr(
        RemoteSession,
        "_stage",
        lambda _session, _files: (_ for _ in ()).throw(startup_error),
    )

    def fail_close(_session):
        raise cleanup_error

    monkeypatch.setattr(RemoteSession, "close", fail_close)

    with pytest.raises(RuntimeError) as raised:
        RemoteSession.open(request)

    assert raised.value is startup_error
    notes = raised.value.__notes__
    assert any("session cleanup failed" in note for note in notes)
    assert any("forward cleanup failed" in note for note in notes)
    assert all("startup failure cleanup also failed" in note for note in notes)


def test_version_query_reports_ssh_failure_status_and_diagnostic(monkeypatch):
    ssh_exit_status = 23

    class FailedCommand(SshCommand):
        def __init__(self):
            super().__init__(("fake-ssh", "-F", "test-config"))

        def run(
            self,
            host: str,
            remote_command: str,
            *,
            input_data: bytes | None = None,
            timeout: float = 15,
        ) -> subprocess.CompletedProcess[bytes]:
            assert host == "target"
            assert "openocd-version" in remote_command
            return subprocess.CompletedProcess(
                remote_command,
                ssh_exit_status,
                b"",
                b"Permission denied while querying remote OpenOCD",
            )

    monkeypatch.setattr(
        backend_module,
        "deploy_helper",
        lambda _ssh_command, _host: DeploymentResult("/helper.py", "digest", False),
    )

    with pytest.raises(SessionError) as raised:
        query_remote_openocd_version(FailedCommand(), "target", ("openocd",))

    message = str(raised.value)
    assert str(ssh_exit_status) in message
    assert "Permission denied" in message


def test_version_query_reports_helper_error_frame(monkeypatch):
    response = encode_message("ERROR", code="HELPER_ERROR", message="version probe failed")

    class FailedCommand(SshCommand):
        def run(self, host, remote_command, *, input_data=None, timeout=15):
            return subprocess.CompletedProcess(remote_command, HELPER_FAILURE_RC, response, b"")

    monkeypatch.setattr(
        backend_module,
        "deploy_helper",
        lambda _ssh_command, _host: DeploymentResult("/helper.py", "digest", False),
    )

    with pytest.raises(SessionError) as raised:
        query_remote_openocd_version(FailedCommand(), "target", ("openocd",))

    message = str(raised.value)
    assert f"({HELPER_FAILURE_RC})" in message
    assert "HELPER_ERROR" in message
    assert "version probe failed" in message


def test_version_query_falls_back_to_stderr_for_malformed_failure_frame(monkeypatch):
    class FailedCommand(SshCommand):
        def run(self, host, remote_command, *, input_data=None, timeout=15):
            return subprocess.CompletedProcess(
                remote_command,
                HELPER_FAILURE_RC,
                b"not a protocol frame\n",
                b"transport failed",
            )

    monkeypatch.setattr(
        backend_module,
        "deploy_helper",
        lambda _ssh_command, _host: DeploymentResult("/helper.py", "digest", False),
    )

    with pytest.raises(SessionError) as raised:
        query_remote_openocd_version(FailedCommand(), "target", ("openocd",))

    message = str(raised.value)
    assert f"({HELPER_FAILURE_RC})" in message
    assert "transport failed" in message


def test_version_query_rejects_response_without_lf(monkeypatch):
    response = encode_message("OPENOCD_VERSION", output="OpenOCD 0.12.0").rstrip(b"\n")

    class ReplyCommand(SshCommand):
        def run(self, host, remote_command, *, input_data=None, timeout=15):
            return subprocess.CompletedProcess(remote_command, 0, response, b"")

    monkeypatch.setattr(
        backend_module,
        "deploy_helper",
        lambda _ssh_command, _host: DeploymentResult("/helper.py", "digest", False),
    )

    with pytest.raises(SessionError):
        query_remote_openocd_version(ReplyCommand(), "target", ("openocd",))


def test_staging_rejects_response_without_lf(monkeypatch):
    response = encode_message(
        "STAGED", byte_count=0, sha256="0" * 64, files=[], directories=[]
    ).rstrip(b"\n")
    deployment = DeploymentResult("/helper.py", "digest", False)

    class ReplyCommand(SshCommand):
        def run_stream(self, host, remote_command, stream, *, timeout=60):
            del host, stream, timeout
            return subprocess.CompletedProcess(remote_command, 0, response, b"")

    class Helper(_BlockedHelper):
        @property
        @override
        def allocation(self) -> SessionAllocation:
            return SessionAllocation("session", "/workspace")

        @override
        def close(self) -> _HelperCloseResult:
            return _HelperCloseResult(None, ())

    def open_helper(_ssh_command, _host, _deployment, *, output_handler=None):
        del output_handler
        return Helper()

    monkeypatch.setattr(
        backend_module,
        "deploy_helper",
        lambda _ssh_command, _host: deployment,
    )
    monkeypatch.setattr(_HelperClient, "open", open_helper)
    request = RemoteSessionRequest("target", ReplyCommand(), RemoteProcess(("openocd",)))

    with pytest.raises(SessionError):
        RemoteSession.open(request)


def test_closed_session_exposes_only_cached_openocd_result():
    class Helper(_BlockedHelper):
        def __init__(self, openocd_returncode: int | None) -> None:
            self._openocd_returncode = openocd_returncode

        @property
        @override
        def openocd_returncode(self) -> int | None:
            return self._openocd_returncode

    session = _make_session()
    session.closed = True
    session._helper = Helper(None)

    assert session.openocd_returncode is None
    assert session.check_openocd_exit() is None
    with pytest.raises(SessionClosedError):
        session.wait_for_openocd_exit()
    with pytest.raises(SessionClosedError):
        session.forward(())

    completed = _make_session()
    completed.closed = True
    completed._helper = Helper(OPENOCD_FAILURE_RC)
    assert completed.openocd_returncode == OPENOCD_FAILURE_RC
    assert completed.check_openocd_exit() == OPENOCD_FAILURE_RC
    assert completed.wait_for_openocd_exit() == OPENOCD_FAILURE_RC


def test_close_attempts_all_cleanup_once_and_preserves_first_failure():
    session = _make_session()
    first_error = RuntimeError("forward cleanup failed")
    later_error = RuntimeError("helper cleanup failed")
    later_error.add_note("helper cleanup also failed: stream close failed")
    actions: list[str] = []

    class Forwards(_BlockedForwards):
        @override
        def close(self) -> None:
            actions.append("forwards")
            raise first_error

    class Helper(_BlockedHelper):
        @override
        def close(self) -> _HelperCloseResult:
            actions.append("helper")
            return _HelperCloseResult(later_error, ())

    session._forwards = Forwards()
    session._helper = Helper()

    with pytest.raises(RuntimeError) as raised:
        session.close()

    assert raised.value is first_error
    notes = raised.value.__notes__
    assert any("helper cleanup failed" in note for note in notes)
    assert any("stream close failed" in note for note in notes)
    assert all("additional cleanup failure" in note for note in notes)
    assert actions == ["forwards", "helper"]
    assert session.closed
    session.close()
    assert actions == ["forwards", "helper"]


def test_close_raises_helper_cleanup_only_error_after_closing_session():
    session = _make_session()
    cleanup_error = RuntimeError("helper stream close failed")
    actions: list[str] = []

    class Forwards(_BlockedForwards):
        @override
        def close(self) -> None:
            actions.append("forwards")

    class Helper(_BlockedHelper):
        @override
        def close(self) -> _HelperCloseResult:
            actions.append("helper")
            return _HelperCloseResult(None, (cleanup_error,))

    session._forwards = Forwards()
    session._helper = Helper()

    with pytest.raises(RuntimeError) as raised:
        session.close()

    assert raised.value is cleanup_error
    assert actions == ["forwards", "helper"]
    assert session.closed


@pytest.mark.timeout(10)
def test_wait_for_openocd_exit_observes_forward_failure():
    class Helper(_BlockedHelper):
        def __init__(self, forwards: Forwards) -> None:
            self.forwards = forwards
            self.wait_timeouts: list[float] = []

        @property
        @override
        def openocd_returncode(self) -> int | None:
            return None

        @override
        def recorded_openocd_exit(self) -> int | None:
            return None

        @override
        def wait_for_change(self, timeout: float | None) -> None:
            assert timeout is not None
            self.wait_timeouts.append(timeout)
            self.forwards.failed = True

    class Forwards(_BlockedForwards):
        def __init__(self) -> None:
            self.failed = False

        @property
        @override
        def has_forwards(self) -> bool:
            return True

        @override
        def check_health(self) -> None:
            if self.failed:
                raise SessionError(f"SSH forwarding exited with status {FORWARD_FAILURE_RC}")

    session = _make_session()
    forwards = Forwards()
    helper = Helper(forwards)
    session._helper = helper
    session._forwards = forwards

    with pytest.raises(SessionError):
        session.wait_for_openocd_exit()
    assert len(helper.wait_timeouts) == 1
    assert 0 < helper.wait_timeouts[0] <= backend_module.FORWARD_HEALTH_INTERVAL


def test_wait_for_openocd_exit_raises_helper_timeout_at_deadline(monkeypatch):
    requested_timeout = 2.5
    clock = [0.0]

    class Helper(_BlockedHelper):
        def __init__(self) -> None:
            self.wait_timeouts: list[float] = []
            self.expired_timeout: float | None = None
            self.timeout_error = subprocess.TimeoutExpired(("python3", "helper"), requested_timeout)

        @property
        @override
        def openocd_returncode(self) -> int | None:
            return None

        @override
        def recorded_openocd_exit(self) -> int | None:
            return None

        @override
        def wait_for_change(self, timeout: float | None) -> None:
            assert timeout is not None and timeout > 0
            self.wait_timeouts.append(timeout)
            clock[0] += timeout

        @override
        def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
            self.expired_timeout = timeout
            return self.timeout_error

    class Forwards(_BlockedForwards):
        @property
        @override
        def has_forwards(self) -> bool:
            return False

        @override
        def check_health(self) -> None:
            pass

    helper = Helper()
    session = _make_session()
    session._helper = helper
    session._forwards = Forwards()
    monkeypatch.setattr(backend_module.time, "monotonic", lambda: clock[0])

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        session.wait_for_openocd_exit(requested_timeout)

    assert raised.value is helper.timeout_error
    assert helper.expired_timeout == requested_timeout
    assert clock[0] == requested_timeout
    assert all(0 < wait <= requested_timeout for wait in helper.wait_timeouts)


def test_forward_uses_allocated_remote_address_and_requested_services():
    remote_address = "127.0.0.7"
    requested_services = (
        Service("gdb", 3333, 3333),
        Service("telnet", 4444, 4444),
    )

    class Forwards(_BlockedForwards):
        def __init__(self) -> None:
            self.forwarded: tuple[tuple[Service, ...], str] | None = None

        @override
        def start(self, services: Iterable[Service], remote_address: str) -> None:
            self.forwarded = (tuple(services), remote_address)

    session = _make_session()
    session.descriptor = SessionDescriptor(
        SessionAllocation("session-id", "/tmp/session"), remote_address
    )
    forwards = Forwards()
    session._forwards = forwards

    session.forward(requested_services)

    assert forwards.forwarded == (requested_services, remote_address)
