# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import math
import subprocess

import pytest
from zephyr_remote_openocd.remote import preferred_address_cache
from zephyr_remote_openocd.remote import ssh as ssh_module
from zephyr_remote_openocd.remote.forwarding import ForwardStartError
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.forwarding_support import GDB, RTT, TCL, TELNET, ControlledSshCommand, ForwardingHarness


@pytest.fixture
def harness(monkeypatch):
    return ForwardingHarness(monkeypatch)


def test_required_start_failure_aborts_session(harness):
    harness.ssh.process(GDB).ready = False
    with pytest.raises(ForwardStartError):
        harness.open()
    assert harness.helper.close_calls == 1
    assert harness.ssh.process(GDB).mock.close_stderr.call_count == 1
    assert not harness.advisories


def test_required_forwarding_saves_preferred_address_after_using_cache(harness):
    preferred_address_cache.remember_preferred_address("host", harness.ssh, "127.64.0.9")
    session = harness.open((GDB,), ())
    try:
        assert harness.helper.preferred_address == "127.64.0.9"
        assert preferred_address_cache.load_preferred_address("host", harness.ssh) == "127.64.0.1"
    finally:
        session.close()


def test_failed_required_forwarding_preserves_cached_preferred_address(harness):
    preferred_address_cache.remember_preferred_address("host", harness.ssh, "127.64.0.9")
    harness.ssh.process(GDB).ready = False
    with pytest.raises(ForwardStartError):
        harness.open((GDB,), ())
    assert harness.helper.preferred_address == "127.64.0.9"
    assert preferred_address_cache.load_preferred_address("host", harness.ssh) == "127.64.0.9"


@pytest.mark.parametrize("services", ((), (TCL,)), ids=("no-services", "best-effort-only"))
def test_session_without_required_forwarding_does_not_save_preferred_address(harness, services):
    session = harness.open(services, services)
    try:
        assert preferred_address_cache.load_preferred_address("host", harness.ssh) is None
    finally:
        session.close()


def test_deferred_required_forwarding_saves_preferred_address(harness):
    session = harness.open((), ())
    harness.ssh.process(RTT)
    try:
        session.forward((RTT,))
        assert preferred_address_cache.load_preferred_address("host", harness.ssh) == "127.64.0.1"
    finally:
        session.close()


@pytest.mark.parametrize("failed_service", (TCL, TELNET, RTT), ids=("tcl", "telnet", "rtt"))
def test_auxiliary_start_failure_preserves_other_services(harness, failed_service):
    harness.ssh.process(failed_service).ready = False
    services = (GDB, TCL, TELNET, RTT)
    session = harness.open(services, (TCL, TELNET, RTT))
    try:
        assert session.forwarded_services == tuple(
            item for item in services if item != failed_service
        )
        assert harness.helper.services == services
        assert session.check_openocd_exit() is None
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
        harness.open()
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

    session = harness.open((GDB,), ())
    monkeypatch.setattr(ControlledSshCommand, "popen", SshCommand.popen)
    monkeypatch.setattr(ssh_module.subprocess, "Popen", lambda *_args, **_kwargs: acquired)
    monkeypatch.setattr(ssh_module._StderrDrain, "start", fail_drain_start)
    try:
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
        harness.open()
    assert raised.value is error
    assert not harness.advisories
    assert harness.helper.close_calls == 1
    assert harness.ssh.process(GDB).mock.close_stderr.call_count == 1
    assert harness.ssh.process(TCL).mock.close_stderr.call_count == 1


@pytest.mark.parametrize("failed_service", (TCL, TELNET, RTT), ids=("tcl", "telnet", "rtt"))
def test_auxiliary_exit_warns_once_without_failing_gdb(harness, failed_service):
    session = harness.open((GDB, TCL, TELNET, RTT), (TCL, TELNET, RTT))
    try:
        harness.ssh.process(failed_service).returncode = 13
        assert session.check_openocd_exit() is None
        assert session.check_openocd_exit() is None
        assert [advisory.service for advisory in harness.advisories] == [failed_service]
        assert harness.advisories[0].phase == "runtime"
        assert harness.ssh.process(GDB).mock.close_stderr.call_count == 0
    finally:
        session.close()


def test_required_exit_remains_fatal_on_repeated_health_checks(harness):
    session = harness.open()
    try:
        harness.ssh.process(GDB).returncode = 13
        for _ in range(2):
            with pytest.raises(SessionError):
                session.check_openocd_exit()
        assert not harness.advisories
    finally:
        session.close()


def test_simultaneous_forward_failures_preserve_required_failure_and_advisories(harness):
    session = harness.open()
    try:
        for service in (GDB, TCL):
            harness.ssh.process(service).returncode = 13
        with pytest.raises(SessionError):
            session.check_openocd_exit()
        assert [advisory.service for advisory in harness.advisories] == [TCL]
        harness.helper.openocd_returncode = 7
        assert session.check_openocd_exit() == 7
    finally:
        session.close()


@pytest.mark.parametrize("unknown", (RTT, type(GDB)("gdb", 3334, 3333)))
def test_mark_auxiliary_requires_owned_service(harness, unknown):
    session = harness.open()
    try:
        with pytest.raises(SessionError):
            session.mark_auxiliary((GDB, unknown))
        harness.ssh.process(GDB).returncode = 13
        with pytest.raises(SessionError):
            session.check_openocd_exit()
    finally:
        session.close()


def test_late_observed_gdb_exit_uses_current_rtt_forwarding_classification(harness):
    session = harness.open()
    try:
        harness.ssh.process(GDB).returncode = 13
        session.mark_auxiliary((GDB,))
        harness.ssh.process(RTT)
        session.forward((RTT,), required=True)
        assert session.check_openocd_exit() is None
        assert [advisory.service for advisory in harness.advisories] == [GDB]
        harness.ssh.process(RTT).returncode = 13
        with pytest.raises(SessionError):
            session.check_openocd_exit()
    finally:
        session.close()


def test_owned_best_effort_cleanup_failure_is_fatal(harness):
    session = harness.open()
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
