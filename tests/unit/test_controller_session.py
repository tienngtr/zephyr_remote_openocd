# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import override

import pytest
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller.authority import SessionClosedError, SessionError
from zephyr_remote_openocd.controller.deployment import DeploymentResult
from zephyr_remote_openocd.controller.forwarding import ForwardFailure
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.plan import SessionAllocation, SessionPlan
from zephyr_remote_openocd.controller.session import SessionController, query_remote_openocd_version
from zephyr_remote_openocd.controller.ssh import SshCommand
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.outcome import Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import ProtocolError, encode_frame

from tests.forwarding_support import ForwardingHarness
from tests.process_support import cleanup_on_exit
from tests.protocol_support import terminal_snapshot

OPENOCD_FAILURE_RC = 7
FORWARD_FAILURE_RC = 13
HELPER_FAILURE_RC = 17


class _BlockedHelper:
    def acquire(self) -> None:
        raise AssertionError("acquire() is not expected")

    @property
    def allocation(self) -> SessionAllocation:
        raise AssertionError("allocation is not expected")

    def start_process(
        self,
        process: ProcessSpec,
        listeners: Iterable[ListenerSpec],
        *,
        preferred_address: str | None = None,
    ) -> str:
        del process, listeners, preferred_address
        raise AssertionError("start_process() is not expected")

    def observe_status(self) -> None:
        pass

    def wait_for_change(self, timeout: float | None) -> None:
        del timeout
        raise AssertionError("wait_for_change() is not expected")

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        del timeout
        raise AssertionError("timeout_expired() is not expected")

    def close(self) -> None:
        raise AssertionError("close() is not expected")


class _PreparedHelper(_BlockedHelper):
    @override
    def acquire(self) -> None:
        pass


class _BlockedForwards:
    @property
    def forwards(self) -> tuple[ForwardSpec, ...]:
        raise AssertionError("services is not expected")

    @property
    def has_forwards(self) -> bool:
        raise AssertionError("has_forwards is not expected")

    def start(self, forwards: Iterable[ForwardSpec], remote_address: str) -> None:
        del forwards, remote_address
        raise AssertionError("start() is not expected")

    def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
        raise AssertionError("check_health() is not expected")

    def close(self) -> None:
        raise AssertionError("close() is not expected")


def _make_session() -> SessionController:
    request = SessionPlan("host", SshCommand(), ProcessSpec(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    return SessionController(request, deployment)


def test_session_validates_reserved_services_without_forwarding_them(monkeypatch):
    harness = ForwardingHarness(monkeypatch)
    gdb = ForwardSpec("gdb", 3333, 3333)
    rtt = ForwardSpec("rtt", 5566, 5566)
    request = replace(harness.request((gdb,), ()), reserved_listeners=(rtt.listener,))
    session = SessionController.prepare(request)
    try:
        session.acquire()
        assert harness.helper.listeners == (gdb.listener, rtt.listener)
        assert session.forwarded_specs == (gdb,)
    finally:
        session.close()


@pytest.mark.parametrize(
    "returncode, stdout, stderr, diagnostics",
    (
        pytest.param(
            23,
            b"",
            b"Permission denied while querying remote OpenOCD",
            ("23", "Permission denied"),
            id="ssh-failure",
        ),
        pytest.param(
            HELPER_FAILURE_RC,
            encode_frame("ERROR", code="HELPER_ERROR", message="version probe failed"),
            b"",
            (str(HELPER_FAILURE_RC), "HELPER_ERROR", "version probe failed"),
            id="helper-error",
        ),
        pytest.param(
            HELPER_FAILURE_RC,
            b"not a protocol frame\n",
            b"transport failed",
            (str(HELPER_FAILURE_RC), "transport failed"),
            id="malformed-failure-frame",
        ),
        pytest.param(
            0,
            encode_frame("OPENOCD_VERSION", output="OpenOCD 0.12.0").rstrip(b"\n"),
            b"",
            (),
            id="missing-lf",
        ),
    ),
)
def test_version_query_rejects_failed_or_malformed_response(
    monkeypatch, returncode, stdout, stderr, diagnostics
):
    class ReplyCommand(SshCommand):
        @override
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
            return subprocess.CompletedProcess(remote_command, returncode, stdout, stderr)

    monkeypatch.setattr(
        session_module,
        "deploy_helper",
        lambda _ssh_command, _host: DeploymentResult("/helper.py", "digest", False),
    )
    with pytest.raises(SessionError) as raised:
        query_remote_openocd_version(
            ReplyCommand(("fake-ssh", "-F", "test-config")), "target", ("openocd",)
        )
    assert all(detail in str(raised.value) for detail in diagnostics)


def test_staging_rejects_response_without_lf(monkeypatch):
    response = encode_frame(
        "STAGED", byte_count=0, sha256=hashlib.sha256(b"").hexdigest(), files=[], directories=[]
    ).rstrip(b"\n")
    deployment = DeploymentResult("/helper.py", "digest", False)

    class ReplyCommand(SshCommand):
        def run_stream(self, host, remote_command, stream, *, timeout=60):
            del host, stream, timeout
            return subprocess.CompletedProcess(remote_command, 0, response, b"")

    class Helper(_PreparedHelper):
        # start_process() inherits the explicit forbidden-operation guard.
        # A framing regression must fail there, rather than at another check.
        @property
        @override
        def allocation(self) -> SessionAllocation:
            return SessionAllocation("session", "/workspace")

        @override
        def close(self) -> None:
            pass

    def make_helper(
        _ssh_command,
        _host,
        _deployment,
        *,
        output_handler=None,
        process_start_handler=None,
        authority=None,
    ):
        del output_handler
        return Helper()

    monkeypatch.setattr(
        session_module,
        "deploy_helper",
        lambda _ssh_command, _host: deployment,
    )
    monkeypatch.setattr(session_module, "HelperConnection", make_helper)
    request = SessionPlan("target", ReplyCommand(), ProcessSpec(("openocd",)))

    with pytest.raises(SessionError) as raised:
        session = SessionController.prepare(request)
        with cleanup_on_exit(session.close):
            session.acquire()
    assert isinstance(raised.value.__cause__, ProtocolError)


def test_closed_session_exposes_only_cached_openocd_result():
    session = _make_session()
    session.closed = True
    session._helper_connection = _BlockedHelper()

    assert session.openocd_returncode is None
    assert session.poll_operation() is None
    with pytest.raises(SessionClosedError):
        session.wait_for_openocd_exit()
    with pytest.raises(SessionClosedError):
        session.forward(())

    completed = _make_session()
    completed.closed = True
    completed._authority.record_terminal(
        terminal_snapshot(Trigger.CHILD_EXIT, returncode=OPENOCD_FAILURE_RC)
    )
    assert completed.openocd_returncode == OPENOCD_FAILURE_RC
    with pytest.raises(SessionError):
        completed.poll_operation()
    with pytest.raises(SessionError):
        completed.wait_for_openocd_exit()


def test_check_openocd_exit_propagates_helper_observation_failure():
    observation_error = SessionError("helper event stream failed")

    class Helper(_BlockedHelper):
        @override
        def observe_status(self) -> None:
            raise observation_error

    session = _make_session()
    session._helper_connection = Helper()

    with pytest.raises(SessionError) as raised:
        session.poll_operation()

    assert raised.value is observation_error


def test_check_openocd_exit_propagates_forward_failure_after_successful_openocd_exit():
    forwarding_error = SessionError(f"SSH forwarding exited with status {FORWARD_FAILURE_RC}")

    class Helper(_BlockedHelper):
        @override
        def observe_status(self) -> None:
            pass

    class Forwards(_BlockedForwards):
        @override
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            raise forwarding_error

    session = _make_session()
    session._authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))
    session._helper_connection = Helper()
    session._forwards = Forwards()

    with pytest.raises(SessionError) as raised:
        session.poll_operation()

    assert raised.value is forwarding_error


@pytest.mark.parametrize("failure_phase", ("health", "disposal"))
def test_close_attempts_all_cleanup_once_and_preserves_first_failure(failure_phase):
    session = _make_session()
    first_error = RuntimeError("forward cleanup failed")
    later_error = RuntimeError("helper cleanup failed")
    later_error.add_note("helper cleanup also failed: stream close failed")
    actions: list[str] = []

    class Forwards(_BlockedForwards):
        @override
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            if failure_phase == "health":
                raise first_error

        @override
        def close(self) -> None:
            actions.append("forwards")
            if failure_phase == "disposal":
                raise first_error

    class Helper(_BlockedHelper):
        @override
        def close(self) -> None:
            actions.append("helper")
            session._authority.record_failure("HELPER_CLEANUP_FAILURE", later_error)

    session._forwards = Forwards()
    session._helper_connection = Helper()

    with pytest.raises(RuntimeError) as raised:
        session.close()

    assert raised.value is first_error
    notes = raised.value.__notes__
    assert any("helper cleanup failed" in note for note in notes)
    assert any("stream close failed" in note for note in notes)
    assert sorted(actions) == ["forwards", "helper"]
    assert session.closed
    session.close()
    assert sorted(actions) == ["forwards", "helper"]


def test_close_raises_helper_cleanup_only_error_after_closing_session():
    session = _make_session()
    cleanup_error = RuntimeError("helper stream close failed")
    actions: list[str] = []

    class Forwards(_BlockedForwards):
        @override
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            return None

        @override
        def close(self) -> None:
            actions.append("forwards")

    class Helper(_BlockedHelper):
        @override
        def close(self) -> None:
            actions.append("helper")
            session._authority.record_failure("HELPER_CLEANUP_FAILURE", cleanup_error)

    session._forwards = Forwards()
    session._helper_connection = Helper()

    with pytest.raises(RuntimeError) as raised:
        session.close()

    assert raised.value is cleanup_error
    assert sorted(actions) == ["forwards", "helper"]
    assert session.closed


@pytest.mark.timeout(10)
def test_wait_for_openocd_exit_observes_forward_failure():
    class Helper(_BlockedHelper):
        def __init__(self, forwards: Forwards) -> None:
            self.forwards = forwards
            self.wait_timeouts: list[float] = []

        @override
        def observe_status(self) -> None:
            pass

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
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            if self.failed:
                raise SessionError(f"SSH forwarding exited with status {FORWARD_FAILURE_RC}")

    session = _make_session()
    forwards = Forwards()
    helper = Helper(forwards)
    session._helper_connection = helper
    session._forwards = forwards

    with pytest.raises(SessionError):
        session.wait_for_openocd_exit()
    assert len(helper.wait_timeouts) == 1
    assert 0 < helper.wait_timeouts[0] <= session_module.FORWARD_HEALTH_INTERVAL
    assert not session.closed


def test_wait_for_openocd_exit_raises_helper_timeout_at_deadline(monkeypatch):
    requested_timeout = 2.5
    clock = [0.0]

    class Helper(_BlockedHelper):
        def __init__(self) -> None:
            self.wait_timeouts: list[float] = []
            self.expired_timeout: float | None = None
            self.timeout_error = subprocess.TimeoutExpired(("python3", "helper"), requested_timeout)

        @override
        def observe_status(self) -> None:
            pass

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
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            return None

    helper = Helper()
    session = _make_session()
    session._helper_connection = helper
    session._forwards = Forwards()
    monkeypatch.setattr(session_module.time, "monotonic", lambda: clock[0])

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        session.wait_for_openocd_exit(requested_timeout)

    assert raised.value is helper.timeout_error
    assert helper.expired_timeout == requested_timeout
    assert clock[0] == requested_timeout
    assert all(0 < wait <= requested_timeout for wait in helper.wait_timeouts)


def test_forward_uses_allocated_remote_address_and_requested_services(monkeypatch):
    harness = ForwardingHarness(monkeypatch)
    requested_forwards = (
        ForwardSpec("gdb", 3333, 3333),
        ForwardSpec("telnet", 4444, 4444),
    )
    session = harness.prepare((), ())
    try:
        session.acquire()
        for forward in requested_forwards:
            harness.ssh.process(forward)
        # The controlled SSH boundary checks the allocated destination address.
        session.forward(requested_forwards)
        assert session.forwarded_specs == requested_forwards
    finally:
        session.close()
