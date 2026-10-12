# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import math
import os
import subprocess
from typing import cast
from unittest.mock import create_autospec

import pytest
from zephyr_remote_openocd.controller import preferred_address_cache
from zephyr_remote_openocd.controller import ssh as ssh_module
from zephyr_remote_openocd.controller.authority import SessionError
from zephyr_remote_openocd.controller.forwarding import ForwardStartError
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.ssh import ManagedSshProcess, SshCommand
from zephyr_remote_openocd.protocol.outcome import Trigger
from zephyr_remote_openocd.protocol.wire import encode_frame

from tests.forwarding_support import GDB, RTT, TCL, TELNET, ControlledSshCommand, ForwardingHarness
from tests.process_support import cleanup_on_exit
from tests.protocol_support import terminal_snapshot


@pytest.fixture
def harness(monkeypatch):
    return ForwardingHarness(monkeypatch)


@pytest.mark.parametrize("fact", ("terminal", "reader"))
@pytest.mark.parametrize("during_cleanup", (False, True), ids=("before-close", "during-close"))
def test_close_preserves_recorded_session_failure_over_forward_cleanup(
    harness, monkeypatch, fact, during_cleanup
):
    session = harness.prepare((GDB,), ())
    with cleanup_on_exit(session.close):
        session.acquire()
        helper = HelperConnection(
            harness.ssh,
            session.request.host,
            session.deployment,
            authority=session._authority,
        )
        process = create_autospec(ManagedSshProcess, instance=True, spec_set=True)
        read_fd, write_fd = os.pipe()
        process.stdout = os.fdopen(read_fd, "rb", buffering=0)
        process.stdin = io.BytesIO()
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.stderr_tail.return_value = b""
        os.write(
            write_fd,
            encode_frame(
                "SESSION_CREATED",
                session_id="session",
                remote_workspace="/workspace",
                helper="fake",
            ),
        )
        os.close(write_fd)
        monkeypatch.setattr(
            ControlledSshCommand,
            "popen",
            lambda *_args, **_kwargs: cast(ManagedSshProcess, process),
        )
        session._helper = helper
        helper.acquire()
        assert session.poll_operation() is None
        cleanup_error = RuntimeError("forward cleanup failed")
        cleanup_error.add_note("nested forward cleanup detail")

        def record_failure():
            if fact == "terminal":
                session._authority.record_terminal(
                    terminal_snapshot(
                        Trigger.HELPER_FAILURE, code="FAILED", message="established failure"
                    )
                )
            else:
                session._authority.record_reader_failure(RuntimeError("established failure"))

        def fail_cleanup():
            if during_cleanup:
                record_failure()
            raise cleanup_error

        harness.ssh.process(GDB).mock.close_stderr.side_effect = fail_cleanup
        if not during_cleanup:
            record_failure()
        with pytest.raises(SessionError) as raised:
            session.close()
        assert "established failure" in str(raised.value)
        assert any(str(cleanup_error) in note for note in raised.value.__notes__)
        assert any("nested forward cleanup detail" in note for note in raised.value.__notes__)
        assert session.closed
        assert process.stdin.closed and process.stdout.closed
        assert harness.ssh.process(GDB).mock.close_stderr.call_count == 1
        session.close()


def test_required_start_failure_aborts_session(harness):
    harness.ssh.process(GDB).ready = False
    with pytest.raises(ForwardStartError):
        session = harness.prepare()
        with cleanup_on_exit(session.close):
            session.acquire()
    assert harness.helper.close_calls == 1
    assert harness.ssh.process(GDB).mock.close_stderr.call_count == 1
    assert not harness.advisories


def test_required_forwarding_saves_preferred_address_after_using_cache(harness):
    preferred_address_cache.remember_preferred_address("host", harness.ssh, "127.64.0.9")
    session = harness.prepare((GDB,), ())
    try:
        session.acquire()
        assert harness.helper.preferred_address == "127.64.0.9"
        assert preferred_address_cache.load_preferred_address("host", harness.ssh) == "127.64.0.1"
    finally:
        session.close()


def test_failed_required_forwarding_preserves_cached_preferred_address(harness):
    preferred_address_cache.remember_preferred_address("host", harness.ssh, "127.64.0.9")
    harness.ssh.process(GDB).ready = False
    with pytest.raises(ForwardStartError):
        session = harness.prepare((GDB,), ())
        with cleanup_on_exit(session.close):
            session.acquire()
    assert harness.helper.preferred_address == "127.64.0.9"
    assert preferred_address_cache.load_preferred_address("host", harness.ssh) == "127.64.0.9"


@pytest.mark.parametrize("services", ((), (TCL,)), ids=("no-services", "best-effort-only"))
def test_session_without_required_forwarding_does_not_save_preferred_address(harness, services):
    session = harness.prepare(services, services)
    try:
        session.acquire()
        assert preferred_address_cache.load_preferred_address("host", harness.ssh) is None
    finally:
        session.close()


def test_deferred_required_forwarding_saves_preferred_address(harness):
    session = harness.prepare((), ())
    try:
        session.acquire()
        harness.ssh.process(RTT)
        session.forward((RTT,))
        assert preferred_address_cache.load_preferred_address("host", harness.ssh) == "127.64.0.1"
    finally:
        session.close()


@pytest.mark.parametrize("fact", ("reader", "cancel"))
@pytest.mark.parametrize("required", (True, False))
def test_recorded_failure_or_cancellation_prevents_forward_entry(
    harness, monkeypatch, fact, required
):
    session = harness.prepare((GDB,), ())
    try:
        session.acquire()
        harness.ssh.process(RTT)

        def forbidden_forward(*_args, **_kwargs):
            pytest.fail("forward transport entered after a recorded fatal fact")

        monkeypatch.setattr(ControlledSshCommand, "popen", forbidden_forward)
        if fact == "reader":
            session._authority.record_reader_failure(RuntimeError("earlier reader failure"))
        else:
            session._authority.cancel()
        with pytest.raises(SessionError) as raised:
            session.forward((RTT,), required=required)
        if fact == "reader":
            assert "earlier reader failure" in str(raised.value)
        assert session.forwarded_services == (GDB,)
        assert not harness.advisories
    finally:
        session.close()


@pytest.mark.parametrize("failed_service", (TCL, TELNET, RTT), ids=("tcl", "telnet", "rtt"))
def test_auxiliary_start_failure_preserves_other_services(harness, failed_service):
    harness.ssh.process(failed_service).ready = False
    services = (GDB, TCL, TELNET, RTT)
    session = harness.prepare(services, (TCL, TELNET, RTT))
    try:
        session.acquire()
        assert session.forwarded_services == tuple(
            item for item in services if item != failed_service
        )
        assert harness.helper.services == services
        assert session.poll_operation() is None
        assert [advisory.service for advisory in harness.advisories] == [failed_service]
        assert harness.advisories[0].phase == "startup"
        assert harness.ssh.process(GDB).mock.close_stderr.call_count == 0
    finally:
        session.close()
    assert harness.ssh.process(failed_service).mock.close_stderr.call_count == 1


def test_best_effort_rollback_failure_remains_fatal(harness):
    failed = harness.ssh.process(TELNET)
    failed.ready = False
    cleanup_error = RuntimeError("rollback cleanup failed")
    failed.cleanup_error = cleanup_error
    with pytest.raises(ForwardStartError) as raised:
        session = harness.prepare()
        with cleanup_on_exit(session.close):
            session.acquire()
    assert raised.value.service == TELNET
    assert raised.value.cleanup_errors == (cleanup_error,)
    assert not harness.advisories
    assert harness.helper.close_calls == 1
    for service in (GDB, TCL, TELNET):
        assert harness.ssh.process(service).mock.close_stderr.call_count == 1


@pytest.mark.parametrize("cleanup_fails", (False, True), ids=("clean-rollback", "failed-rollback"))
def test_auxiliary_factory_rollback_outcome_is_preserved(harness, monkeypatch, cleanup_fails):
    startup_error = OSError("diagnostic reader startup failed")
    cleanup_error = OSError("acquired SSH process kill failed")

    class AcquiredProcess:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None
            self.kill_calls = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.kill_calls += 1
            if cleanup_fails:
                raise cleanup_error
            self.returncode = 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            assert timeout is not None and math.isfinite(timeout) and timeout > 0
            if self.returncode is None:
                raise subprocess.TimeoutExpired("configured-ssh", timeout)
            return self.returncode

    acquired = AcquiredProcess()

    def fail_drain_start(_drain):
        raise startup_error

    session = harness.prepare((GDB,), ())
    try:
        session.acquire()
        monkeypatch.setattr(ControlledSshCommand, "popen", SshCommand.popen)
        monkeypatch.setattr(ssh_module.subprocess, "Popen", lambda *_args, **_kwargs: acquired)
        monkeypatch.setattr(ssh_module._StderrDrain, "start", fail_drain_start)
        if cleanup_fails:
            with pytest.raises(ForwardStartError) as raised:
                session.forward((TCL,), required=False)
            assert cleanup_error in raised.value.cleanup_errors
            assert any(
                isinstance(error, subprocess.TimeoutExpired)
                for error in raised.value.cleanup_errors
            )
            assert not harness.advisories
        else:
            session.forward((TCL,), required=False)
            assert [advisory.service for advisory in harness.advisories] == [TCL]
        assert session.forwarded_services == (GDB,)
    finally:
        session.close()
    assert acquired.kill_calls == 1
    assert acquired.stdin.closed and acquired.stdout.closed and acquired.stderr.closed
    assert harness.helper.close_calls == 1


@pytest.mark.parametrize("error", (KeyboardInterrupt(), RuntimeError("unexpected failure")))
def test_auxiliary_start_interruption_or_unexpected_error_is_not_advisory(harness, error):
    harness.ssh.process(TCL).readiness_error = error
    with pytest.raises(type(error)) as raised:
        session = harness.prepare()
        with cleanup_on_exit(session.close):
            session.acquire()
    assert raised.value is error
    assert not harness.advisories
    assert harness.helper.close_calls == 1
    assert harness.ssh.process(GDB).mock.close_stderr.call_count == 1
    assert harness.ssh.process(TCL).mock.close_stderr.call_count == 1


@pytest.mark.parametrize("failed_service", (TCL, TELNET, RTT), ids=("tcl", "telnet", "rtt"))
def test_auxiliary_exit_warns_once_without_failing_gdb(harness, failed_service):
    session = harness.prepare((GDB, TCL, TELNET, RTT), (TCL, TELNET, RTT))
    try:
        session.acquire()
        harness.ssh.process(failed_service).returncode = 13
        assert session.poll_operation() is None
        assert session.poll_operation() is None
        assert [advisory.service for advisory in harness.advisories] == [failed_service]
        assert harness.advisories[0].phase == "runtime"
        assert harness.ssh.process(GDB).mock.close_stderr.call_count == 0
    finally:
        session.close()


def test_required_exit_remains_fatal_on_repeated_health_checks(harness):
    session = harness.prepare()
    try:
        session.acquire()
        harness.ssh.process(GDB).returncode = 13
        for _ in range(2):
            with pytest.raises(SessionError):
                session.poll_operation()
        assert not harness.advisories
    finally:
        session.close()


@pytest.mark.parametrize("observation", ("same-poll", "later-poll", "close"))
def test_multiple_required_forward_failures_retain_secondary_diagnostics(harness, observation):
    session = harness.prepare((GDB, RTT), ())
    try:
        session.acquire()
        gdb_forward = harness.ssh.process(GDB)
        rtt_forward = harness.ssh.process(RTT)
        gdb_forward.diagnostic = b"gdb transport failed"
        rtt_forward.diagnostic = b"rtt transport failed"
        gdb_forward.returncode = 13
        if observation == "later-poll":
            with pytest.raises(SessionError) as first:
                session.poll_operation()
        rtt_forward.returncode = 17
        with pytest.raises(SessionError) as raised:
            if observation == "close":
                session.close()
            else:
                session.poll_operation()
        primary = raised.value
        assert gdb_forward.diagnostic.decode() in str(primary)
        notes = tuple(getattr(primary, "__notes__", ()))
        assert sum(rtt_forward.diagnostic.decode() in note for note in notes) == 1
        assert any(RTT.name in note and "17" in note for note in notes)
        if observation == "later-poll":
            assert primary is first.value
        if observation != "close":
            with pytest.raises(SessionError) as repeated:
                session.poll_operation()
            assert repeated.value is primary
            assert tuple(primary.__notes__) == notes
        assert not harness.advisories
    finally:
        session.close()
    assert gdb_forward.mock.close_stderr.call_count == 1
    assert rtt_forward.mock.close_stderr.call_count == 1


def test_simultaneous_forward_failures_preserve_required_failure_and_advisories(harness):
    session = harness.prepare()
    try:
        session.acquire()
        for service in (GDB, TCL):
            harness.ssh.process(service).returncode = 13
        with pytest.raises(SessionError) as first:
            session.poll_operation()
        assert [advisory.service for advisory in harness.advisories] == [TCL]
        harness.helper.authority.record_terminal(
            terminal_snapshot(Trigger.CHILD_EXIT, returncode=7)
        )
        with pytest.raises(SessionError) as later:
            session.poll_operation()
        assert later.value is first.value
        assert session.openocd_returncode == 7
        assert any("exit status 7" in note for note in later.value.__notes__)
    finally:
        session.close()


@pytest.mark.parametrize("unknown", (RTT, type(GDB)("gdb", 3334, 3333)))
def test_mark_auxiliary_requires_owned_service(harness, unknown):
    session = harness.prepare()
    try:
        session.acquire()
        with pytest.raises(SessionError):
            session.make_forwards_optional((GDB, unknown))
        harness.ssh.process(GDB).returncode = 13
        with pytest.raises(SessionError):
            session.poll_operation()
    finally:
        session.close()


def test_late_observed_gdb_exit_uses_current_rtt_forwarding_classification(harness):
    session = harness.prepare()
    try:
        session.acquire()
        harness.ssh.process(GDB).returncode = 13
        session.make_forwards_optional((GDB,))
        harness.ssh.process(RTT)
        session.forward((RTT,), required=True)
        assert session.poll_operation() is None
        assert [advisory.service for advisory in harness.advisories] == [GDB]
        harness.ssh.process(RTT).returncode = 13
        with pytest.raises(SessionError):
            session.poll_operation()
    finally:
        session.close()


def test_owned_best_effort_cleanup_failure_is_fatal(harness):
    session = harness.prepare()
    with cleanup_on_exit(session.close):
        session.acquire()
        cleanup_error = RuntimeError("owned best-effort cleanup failed")
        harness.ssh.process(TCL).cleanup_error = cleanup_error
        with pytest.raises(RuntimeError) as raised:
            session.close()
        assert raised.value is cleanup_error
        assert harness.helper.close_calls == 1
        for service in (GDB, TCL, TELNET):
            assert harness.ssh.process(service).mock.close_stderr.call_count == 1
        session.close()
        assert harness.ssh.process(TCL).mock.close_stderr.call_count == 1


def test_terminal_recorded_during_forward_health_collection_remains_primary(harness):
    session = harness.prepare((GDB,), ())
    try:
        session.acquire()
        process = harness.ssh.process(GDB)

        def observe_forward_exit():
            harness.helper.authority.record_terminal(
                terminal_snapshot(Trigger.CHILD_EXIT, returncode=7)
            )
            return 13

        process.mock.poll.side_effect = observe_forward_exit
        with pytest.raises(SessionError, match="exit status 7") as first:
            session.poll_operation()
        assert any("gdb" in note and "13" in note for note in first.value.__notes__)
        with pytest.raises(SessionError) as later:
            session.poll_operation()
        assert later.value is first.value
        assert session.openocd_returncode == 7
    finally:
        # The health observation happened already; physical cleanup succeeds.
        process.mock.poll.side_effect = lambda: process.returncode
        session.close()


def test_forward_exit_is_published_before_a_later_poll_failure(harness):
    session = harness.prepare((GDB, TCL), ())
    later_error = OSError("later Tcl poll failed")
    later_error.add_note("poll diagnostic detail")
    try:
        session.acquire()
        harness.ssh.process(GDB).returncode = 13
        tcl = harness.ssh.process(TCL)
        tcl.mock.poll.side_effect = later_error
        with pytest.raises(SessionError) as first:
            session.poll_operation()
        assert "gdb" in str(first.value) and "13" in str(first.value)
        assert any(str(later_error) in note for note in first.value.__notes__)
        assert any("poll diagnostic detail" in note for note in first.value.__notes__)
        with pytest.raises(SessionError) as later:
            session.poll_operation()
        assert later.value is first.value
    finally:
        tcl.mock.poll.side_effect = lambda: tcl.returncode
        session.close()


def test_best_effort_start_cannot_downgrade_an_established_forward_start_failure(harness):
    session = harness.prepare((GDB,), ())
    earlier = ForwardStartError(GDB, OSError("earlier required forward failure"), ())
    try:
        session.acquire()
        process = harness.ssh.process(TCL)

        def fail_readiness(_process, _sentinel, _deadline):
            session.record_operation_failure(earlier)
            raise OSError("later best-effort readiness failure")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "zephyr_remote_openocd.controller.forwarding.ForwardManager._await_ready",
                staticmethod(fail_readiness),
            )
            with pytest.raises(ForwardStartError) as raised:
                session.forward((TCL,), required=False)
        assert raised.value is earlier
        assert any("later best-effort readiness failure" in note for note in earlier.__notes__)
        assert not harness.advisories
        assert process.returncode is not None
    finally:
        session.close()
