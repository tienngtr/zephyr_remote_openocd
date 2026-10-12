# SPDX-License-Identifier: Apache-2.0

"""Native cancellation capture, ownership handoffs, and outcome publication."""

from __future__ import annotations

import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.controller import staging as staging_module
from zephyr_remote_openocd.controller.authority import SessionAuthority, SessionError
from zephyr_remote_openocd.controller.cancellation import (
    checkpoint,
    current_cancellation,
    defer_cancellation,
    interactive_client,
    local_cancellation,
)
from zephyr_remote_openocd.controller.staging_spec import StagedFile

from tests.cancellation_support import deliver_sigint_in_gc
from tests.forwarding_support import GDB, ForwardingHarness

pytestmark = pytest.mark.usefixtures("enabled_cyclic_gc")


@pytest.mark.parametrize("earlier_failure", (False, True))
def test_gc_callback_cancellation_blocks_dependent_work_and_preserves_primary(earlier_failure):
    authority = SessionAuthority()
    authority.record_ready()
    primary = SessionError("already recognized transport failure")
    if earlier_failure:
        authority.record_failure("TRANSPORT_FAILURE", primary)
    expected = SessionError if earlier_failure else KeyboardInterrupt
    with pytest.raises(expected) as raised, local_cancellation() as scope:
        assert scope is not None
        deliver_sigint_in_gc()
        assert scope.interrupted
        authority.admit_work((), ())
        pytest.fail("cancelled operation admitted dependent work")
    if earlier_failure:
        assert raised.value is primary
        assert any(item.code == "LOCAL_CANCELLATION" for item in authority.snapshot().diagnostics)
    else:
        assert scope is not None
        assert raised.value is scope.error
    assert authority.publish_failure() is raised.value


def test_masked_handoff_and_deferred_cleanup_retain_the_signal_until_dispatch():
    previous_handler = signal.getsignal(signal.SIGINT)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    expected_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    settled = []
    try:
        with pytest.raises(KeyboardInterrupt), local_cancellation():
            handoff_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            try:
                deliver_sigint_in_gc()
                checkpoint()
                settled.append("adopted")
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, handoff_mask)
            with defer_cancellation():
                checkpoint()
                settled.extend(("forward-cleaned", "helper-cleaned"))
            checkpoint()
        assert settled == ["adopted", "forward-cleaned", "helper-cleaned"]
        assert signal.getsignal(signal.SIGINT) is previous_handler
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == expected_mask
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def test_nested_scopes_share_the_latch_and_workers_cannot_replace_handlers():
    with pytest.raises(KeyboardInterrupt), local_cancellation() as outer:
        handler = signal.getsignal(signal.SIGINT)

        def worker():
            assert current_cancellation() is None
            with local_cancellation() as worker_scope:
                assert worker_scope is None
                assert current_cancellation() is None

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(worker).result(timeout=30)
        assert signal.getsignal(signal.SIGINT) == handler
        with local_cancellation() as inner:
            assert inner is outer
            deliver_sigint_in_gc()
        pytest.fail("nested cancellation was lost")


def test_interactive_client_preserves_prior_handler_and_does_not_latch_gdb_ctrl_c():
    calls = []
    previous_handler = signal.getsignal(signal.SIGINT)

    def caller_handler(signum, _frame):
        calls.append((signum, threading.current_thread()))

    signal.signal(signal.SIGINT, caller_handler)
    try:
        with pytest.raises(KeyboardInterrupt), local_cancellation() as scope:
            assert scope is not None
            with interactive_client():
                assert signal.getsignal(signal.SIGINT) is caller_handler
                signal.raise_signal(signal.SIGINT)
            assert calls == [(signal.SIGINT, threading.main_thread())]
            assert not scope.interrupted
            deliver_sigint_in_gc()
            checkpoint()
        assert signal.getsignal(signal.SIGINT) is caller_handler
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def test_acquisition_failure_precedes_cancellation_latched_during_rollback():
    authority = SessionAuthority()
    acquisition_error = OSError("transport acquisition failed")
    with pytest.raises(OSError) as raised, local_cancellation():
        # The physical owner has caught the failure, then a signal arrives
        # while rollback settles resources before authority dispatch.
        deliver_sigint_in_gc()
        raise authority.resolve_effect_failure(acquisition_error)
    assert raised.value is acquisition_error
    assert any(item.code == "LOCAL_CANCELLATION" for item in authority.snapshot().diagnostics)


def test_cancellation_before_close_preserves_cleanup_errors_and_settles_both_owners(monkeypatch):
    harness = ForwardingHarness(monkeypatch)
    session = harness.prepare()
    session.acquire()
    harness.ssh.process(GDB).cleanup_error = OSError("forward cleanup failed")
    with pytest.raises(KeyboardInterrupt) as raised, local_cancellation():
        deliver_sigint_in_gc()
        session.close()
    assert session.closed
    assert harness.helper.close_calls == 1
    assert harness.ssh.process(GDB).returncode is not None
    assert any("forward cleanup failed" in note for note in raised.value.__notes__)


def test_archive_cancellation_closes_the_spool_before_publication(tmp_path, monkeypatch):
    source = tmp_path / "firmware.bin"
    source.write_bytes(b"staged contents")
    original_stat = Path.stat
    original_spool = staging_module.tempfile.SpooledTemporaryFile
    spools = []
    delivered = False

    def stat_and_interrupt(path, *args, **kwargs):
        nonlocal delivered
        result = original_stat(path, *args, **kwargs)
        if path == source and not delivered:
            delivered = True
            deliver_sigint_in_gc()
        return result

    def spool(*args, **kwargs):
        stream = original_spool(*args, **kwargs)
        spools.append(stream)
        return stream

    monkeypatch.setattr(Path, "stat", stat_and_interrupt)
    monkeypatch.setattr(staging_module.tempfile, "SpooledTemporaryFile", spool)
    with pytest.raises(KeyboardInterrupt), local_cancellation():
        staging_module.build_archive((StagedFile(source, PurePosixPath("firmware.bin")),))
        pytest.fail("archive published after cancellation was latched")
    assert delivered and spools and all(stream.closed for stream in spools)
