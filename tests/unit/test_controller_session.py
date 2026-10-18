# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import BinaryIO, override

import pytest
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._forwarding import ForwardFailure
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionAllocation,
    SessionClosedError,
    SessionController,
    SessionError,
    SessionPlan,
    SshCommand,
    query_remote_openocd_version,
)
from zephyr_remote_openocd.controller.authority import SessionAuthority
from zephyr_remote_openocd.controller.helper_connection import OutputHandler, ProcessStartHandler
from zephyr_remote_openocd.controller.waiting import (
    Notifier,
    ProcessHandle,
    WaitResult,
    WaitSources,
    wait_for,
)
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.outcome import Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import ProtocolError, encode_frame

from tests.forwarding_support import ControlledHelper, ForwardingHarness
from tests.process_support import cleanup_on_exit
from tests.protocol_support import terminal_snapshot

OPENOCD_FAILURE_RC = 7
FORWARD_FAILURE_RC = 13
HELPER_FAILURE_RC = 17


class _BlockedHelper(ControlledHelper):
    def observe_status(self) -> None:
        pass

    def live_processes(self) -> tuple[ProcessHandle, ...]:
        raise AssertionError("live_processes() is not expected")

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        del timeout
        raise AssertionError("timeout_expired() is not expected")

    def close(self) -> None:
        pass


class _PreparedHelper(_BlockedHelper):
    @override
    def acquire(self) -> None:
        pass


class _BlockedForwards:
    @property
    def forwards(self) -> tuple[ForwardSpec, ...]:
        return ()

    def live_processes(self) -> tuple[ProcessHandle, ...]:
        raise AssertionError("live_processes() is not expected")

    def start(self, forwards: Iterable[ForwardSpec], remote_address: str) -> None:
        del remote_address
        assert not tuple(forwards)

    def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
        raise AssertionError("check_health() is not expected")

    def close(self) -> None:
        pass


def _make_session(
    monkeypatch: pytest.MonkeyPatch,
    helper: ControlledHelper | None = None,
    forwards: _BlockedForwards | None = None,
) -> SessionController:
    """Acquire through the normal coordinator with controlled transport facts."""
    harness = ForwardingHarness(monkeypatch)
    monkeypatch.setattr(session_module, "wait_for", wait_for)
    if helper is not None:
        harness.helper = helper
    if forwards is not None:
        monkeypatch.setattr(session_module, "ForwardManager", lambda *_args: forwards)
    session = harness.prepare((), ())
    session.acquire()
    return session


def test_session_validates_reserved_services_without_forwarding_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    diagnostics: tuple[str, ...],
) -> None:
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


def test_staging_rejects_response_without_lf(monkeypatch: pytest.MonkeyPatch) -> None:
    response = encode_frame(
        "STAGED", byte_count=0, sha256=hashlib.sha256(b"").hexdigest(), files=[], directories=[]
    ).rstrip(b"\n")
    deployment = DeploymentResult("/helper.py", "digest", False)

    class ReplyCommand(SshCommand):
        @override
        def run_stream(
            self, host: str, remote_command: str, stream: BinaryIO, *, timeout: float = 60
        ) -> subprocess.CompletedProcess[bytes]:
            del host, stream, timeout
            return subprocess.CompletedProcess(remote_command, 0, response, b"")

    class Helper(_PreparedHelper):
        allocation = SessionAllocation("session", "/workspace")

        @override
        def start_process(
            self,
            process: ProcessSpec,
            listeners: Iterable[ListenerSpec],
            *,
            preferred_address: str | None = None,
        ) -> str:
            raise AssertionError("invalid staging response must prevent START")

        @override
        def close(self) -> None:
            pass

    def make_helper(
        _ssh_command: SshCommand,
        _host: str,
        _deployment: DeploymentResult,
        *,
        output_handler: OutputHandler | None = None,
        process_start_handler: ProcessStartHandler | None = None,
        authority: SessionAuthority | None = None,
        notifier: Notifier | None = None,
    ) -> Helper:
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


def test_closed_session_exposes_only_cached_openocd_result(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = ForwardingHarness(monkeypatch)
    session = harness.prepare((), ())
    session.acquire()
    session.close()

    def unexpected_observation() -> None:
        raise AssertionError("a closed session must not observe the helper")

    with monkeypatch.context() as closed:
        closed.setattr(harness.helper, "observe_status", unexpected_observation)
        assert session.openocd_returncode is None
        assert session.poll_operation() is None
        with pytest.raises(SessionClosedError):
            session.wait_for_openocd_exit()
        with pytest.raises(SessionClosedError):
            session.forward(())

    completed = harness.prepare((), ())
    completed.acquire()
    harness.helper.authority.record_terminal(
        terminal_snapshot(Trigger.CHILD_EXIT, returncode=OPENOCD_FAILURE_RC)
    )
    with pytest.raises(SessionError):
        completed.close()
    with monkeypatch.context() as closed:
        closed.setattr(harness.helper, "observe_status", unexpected_observation)
        assert completed.openocd_returncode == OPENOCD_FAILURE_RC
        with pytest.raises(SessionError):
            completed.poll_operation()
        with pytest.raises(SessionError):
            completed.wait_for_openocd_exit()


def test_check_openocd_exit_propagates_helper_observation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation_error = SessionError("helper event stream failed")
    active = False

    class Helper(_BlockedHelper):
        @override
        def observe_status(self) -> None:
            if active:
                raise observation_error

    session = _make_session(monkeypatch, Helper())
    active = True

    with pytest.raises(SessionError) as raised:
        session.poll_operation()

    assert raised.value is observation_error
    session.close()


def test_check_openocd_exit_propagates_forward_failure_after_successful_openocd_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarding_error = SessionError(f"SSH forwarding exited with status {FORWARD_FAILURE_RC}")
    active = False

    class Helper(_BlockedHelper):
        @override
        def observe_status(self) -> None:
            pass

    class Forwards(_BlockedForwards):
        @override
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            if active:
                raise forwarding_error

    helper = Helper()
    session = _make_session(monkeypatch, helper, Forwards())
    helper.authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))
    active = True

    with pytest.raises(SessionError) as raised:
        session.poll_operation()

    assert raised.value is forwarding_error
    session.close()


@pytest.mark.parametrize("failure_phase", ("health", "disposal"))
def test_close_attempts_all_cleanup_once_and_preserves_first_failure(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    first_error = RuntimeError("forward cleanup failed")
    later_error = RuntimeError("helper cleanup failed")
    later_error.add_note("helper cleanup also failed: stream close failed")
    actions: list[str] = []
    active = False

    class Forwards(_BlockedForwards):
        @override
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            if active and failure_phase == "health":
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
            self.authority.record_failure("HELPER_CLEANUP_FAILURE", later_error)

    session = _make_session(monkeypatch, Helper(), Forwards())
    active = True

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


def test_close_raises_helper_cleanup_only_error_after_closing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            self.authority.record_failure("HELPER_CLEANUP_FAILURE", cleanup_error)

    session = _make_session(monkeypatch, Helper(), Forwards())

    with pytest.raises(RuntimeError) as raised:
        session.close()

    assert raised.value is cleanup_error
    assert sorted(actions) == ["forwards", "helper"]
    assert session.closed


@pytest.mark.timeout(60)
def test_wait_for_openocd_exit_wakes_on_forward_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    forward_process = subprocess.Popen(
        (sys.executable, "-c", "import sys; sys.stdin.read()"), stdin=subprocess.PIPE
    )

    active = False
    forwarding_error = SessionError(f"SSH forwarding exited with status {FORWARD_FAILURE_RC}")

    class Helper(_BlockedHelper):
        @override
        def observe_status(self) -> None:
            # The forward exits only after the first health observation, so the
            # wait can end only through its process-completion source.
            assert forward_process.stdin is not None
            if active:
                forward_process.stdin.close()

        @override
        def live_processes(self) -> tuple[ProcessHandle, ...]:
            return ()

    class Forwards(_BlockedForwards):
        @override
        def live_processes(self) -> tuple[ProcessHandle, ...]:
            return () if forward_process.returncode is not None else (forward_process,)

        @override
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            if forward_process.poll() is not None:
                raise forwarding_error

    session = _make_session(monkeypatch, Helper(), Forwards())
    active = True
    try:
        with pytest.raises(SessionError, match=f"status {FORWARD_FAILURE_RC}"):
            session.wait_for_openocd_exit()
        assert not session.closed
    finally:
        forward_process.wait(30)
        session.close()


def test_wait_for_openocd_exit_raises_helper_timeout_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_timeout = 15.0

    class Helper(_BlockedHelper):
        def __init__(self) -> None:
            super().__init__()
            self.expired_timeout: float | None = None
            self.timeout_error = subprocess.TimeoutExpired(("python3", "helper"), requested_timeout)

        @override
        def observe_status(self) -> None:
            pass

        @override
        def live_processes(self) -> tuple[ProcessHandle, ...]:
            return ()

        @override
        def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
            self.expired_timeout = timeout
            return self.timeout_error

    class Forwards(_BlockedForwards):
        @override
        def live_processes(self) -> tuple[ProcessHandle, ...]:
            return ()

        @override
        def check_health(self, report: Callable[[ForwardFailure], None]) -> None:
            return None

    helper = Helper()
    session = _make_session(monkeypatch, helper, Forwards())
    now = 100.0

    def expire(*, sources: WaitSources, deadline: float) -> WaitResult:
        nonlocal now
        assert deadline == now + requested_timeout
        now = deadline
        return WaitResult(frozenset(), frozenset(), frozenset(), False, True)

    monkeypatch.setattr(session_module.time, "monotonic", lambda: now)
    monkeypatch.setattr(session_module, "wait_for", expire)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        session.wait_for_openocd_exit(requested_timeout)

    assert raised.value is helper.timeout_error
    assert helper.expired_timeout == requested_timeout
    session.close()


def test_forward_uses_allocated_remote_address_and_requested_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
