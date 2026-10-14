# SPDX-License-Identifier: Apache-2.0

"""Durable Ctrl-C capture and the event-driven wait with real signals."""

from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from zephyr_remote_openocd.controller.cancellation import (
    OperationCancellation,
    OperationCancelled,
    current_operation,
    operation_scope,
)
from zephyr_remote_openocd.controller.waiting import Notifier, WaitSources, wait_for

pytestmark = [pytest.mark.local, pytest.mark.skipif(os.name != "posix", reason="POSIX signals")]

# Generous deadlock guards only; successful tests never wait for them.
GUARD = 30.0
DEADLOCK_GUARD = 120


@contextmanager
def idle_pipe() -> Iterator[int]:
    """A readable descriptor that never becomes ready while open."""
    read_fd, write_fd = os.pipe()
    try:
        yield read_fd
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _raise_sigint() -> None:
    signal.raise_signal(signal.SIGINT)


def installed_wakeup_fd() -> int:
    """Read the process-wide wakeup descriptor through the public API."""
    current = signal.set_wakeup_fd(-1)
    signal.set_wakeup_fd(current, warn_on_full_buffer=False)
    return current


def test_sigint_latches_durably_and_dispatches_at_the_outer_boundary() -> None:
    previous = signal.getsignal(signal.SIGINT)
    with pytest.raises(OperationCancelled), operation_scope() as operation:
        assert operation.capturing
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        # Delivery neither raised nor reset the fact; drain hints change nothing.
        assert operation.requested
        operation.drain_native_wakeup()
        assert operation.requested
    assert signal.getsignal(signal.SIGINT) is previous
    assert installed_wakeup_fd() == -1


def test_wait_dispatches_a_signal_delivered_before_registration() -> None:
    with pytest.raises(OperationCancelled), operation_scope(), idle_pipe() as idle:
        signal.raise_signal(signal.SIGINT)
        wait_for(readable=(idle,), deadline=time.monotonic() + GUARD)
        pytest.fail("cancellation was not dispatched")


class _PollHook:
    """Run an action after wait registration, immediately before sleeping."""

    def __init__(self, action) -> None:
        self.poller = _REAL_POLL()
        self.action = action

    def register(self, descriptor: int, mask: int) -> None:
        self.poller.register(descriptor, mask)

    def poll(self, timeout: int | None) -> list[tuple[int, int]]:
        self.action()
        return self.poller.poll(timeout)


_REAL_POLL = select.poll


@pytest.mark.timeout(DEADLOCK_GUARD)
def test_signal_between_snapshot_and_sleep_wakes_the_wait(monkeypatch) -> None:
    with pytest.raises(OperationCancelled), operation_scope(), idle_pipe() as idle:
        # Registration and the entry dispatch check are already complete.
        monkeypatch.setattr(select, "poll", lambda: _PollHook(_raise_sigint))
        wait_for(readable=(idle,))
        pytest.fail("cancellation was not dispatched after wakeup")


@pytest.mark.timeout(DEADLOCK_GUARD)
def test_native_delivery_to_a_worker_thread_wakes_the_main_thread_wait(monkeypatch) -> None:
    sleeping = threading.Event()

    def deliver_to_this_thread() -> None:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
        assert sleeping.wait(DEADLOCK_GUARD)
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)

    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    worker = threading.Thread(target=deliver_to_this_thread)
    worker.start()
    try:
        with pytest.raises(OperationCancelled), operation_scope(), idle_pipe() as idle:
            # The main thread never receives native SIGINT and has no deadline,
            # so only the native wakeup descriptor can end its poll().
            monkeypatch.setattr(select, "poll", lambda: _PollHook(sleeping.set))
            wait_for(readable=(idle,))
            pytest.fail("worker-thread delivery did not dispatch cancellation")
    finally:
        sleeping.set()
        worker.join(DEADLOCK_GUARD)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def test_full_wakeup_buffer_retains_the_cancellation_fact() -> None:
    with pytest.raises(OperationCancelled), operation_scope() as operation, idle_pipe() as idle:
        write_fd = installed_wakeup_fd()
        with pytest.raises(BlockingIOError):
            while True:
                os.write(write_fd, b"\x00" * 65536)
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        assert operation.requested
        wait_for(readable=(idle,), deadline=time.monotonic() + GUARD)
        pytest.fail("cancellation was not dispatched")


def test_wakeup_bytes_are_hints_not_cancellation_facts() -> None:
    with operation_scope() as operation, idle_pipe() as idle:
        os.write(installed_wakeup_fd(), bytes((signal.SIGINT,)))
        result = wait_for(readable=(idle,), deadline=time.monotonic() + GUARD)
        assert not result.readable and not result.expired
        assert not operation.requested
        # The consumed hint does not wake later waits.
        assert wait_for(readable=(idle,), deadline=time.monotonic()).expired


def test_wait_reports_read_and_write_readiness_separately() -> None:
    left, right = socket.socketpair()
    with left, right:
        result = wait_for(readable=(left,), writable=(left,), deadline=time.monotonic() + GUARD)
        assert result.writable == {left.fileno()}
        assert not result.readable
        right.send(b"x")
        result = wait_for(readable=(left,), writable=(left,), deadline=time.monotonic() + GUARD)
        assert result.readable == {left.fileno()}
        assert result.writable == {left.fileno()}


def test_notification_after_snapshot_survives_until_the_next_wait() -> None:
    notifier = Notifier()
    notifier.open()
    sources = WaitSources(notifiers=(notifier,))
    try:
        # A producer publishes after the consumer's snapshot but before it sleeps.
        notifier.notify()
        notifier.notify()
        assert wait_for(sources=sources, deadline=time.monotonic() + GUARD).notified
        # Coalesced hints were consumed on wakeup, not before the snapshot.
        assert not wait_for(sources=sources, deadline=time.monotonic()).notified
    finally:
        notifier.close()
    notifier.notify()


def test_notification_from_a_worker_wakes_a_blocked_wait() -> None:
    notifier = Notifier()
    notifier.open()
    published: list[str] = []

    def publish() -> None:
        published.append("fact")
        notifier.notify()

    worker = threading.Thread(target=publish)
    try:
        worker.start()
        while not published:
            result = wait_for(
                sources=WaitSources(notifiers=(notifier,)), deadline=time.monotonic() + GUARD
            )
            assert not result.expired
    finally:
        worker.join(GUARD)
        notifier.close()
    assert published == ["fact"]


@pytest.mark.parametrize("pidfd_available", (True, False), ids=("pidfd", "waitid-fallback"))
def test_process_exit_before_registration_is_observed_without_reaping(
    monkeypatch, pidfd_available
) -> None:
    process = subprocess.Popen((sys.executable, "-c", "pass"))
    try:
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        if not pidfd_available:

            def unavailable(_pid: int) -> int:
                raise OSError("pidfd unavailable")

            monkeypatch.setattr(os, "pidfd_open", unavailable)
        result = wait_for(
            sources=WaitSources(processes=(process,)), deadline=time.monotonic() + GUARD
        )
        assert result.exited == {process.pid}
        assert process.returncode is None
    finally:
        assert process.wait(GUARD) == 0


@pytest.mark.parametrize("pidfd_available", (True, False), ids=("pidfd", "waitid-fallback"))
def test_process_exit_during_wait_is_observed(monkeypatch, pidfd_available) -> None:
    if not pidfd_available:

        def unavailable(_pid: int) -> int:
            raise OSError("pidfd unavailable")

        monkeypatch.setattr(os, "pidfd_open", unavailable)
    process = subprocess.Popen(
        (sys.executable, "-c", "import sys; sys.stdin.read()"), stdin=subprocess.PIPE
    )
    try:
        with idle_pipe() as idle:
            assert process.stdin is not None
            first = wait_for(
                readable=(idle,),
                sources=WaitSources(processes=(process,)),
                deadline=time.monotonic(),
            )
            assert not first.exited
            process.stdin.close()
            while not (
                result := wait_for(
                    readable=(idle,),
                    sources=WaitSources(processes=(process,)),
                    deadline=time.monotonic() + GUARD,
                )
            ).exited:
                assert not result.expired
            assert result.exited == {process.pid}
    finally:
        assert process.wait(GUARD) == 0


def test_settlement_retains_repeated_cancellation_without_dispatching() -> None:
    with pytest.raises(OperationCancelled), operation_scope() as operation, idle_pipe() as idle:
        signal.raise_signal(signal.SIGINT)
        assert operation.begin_closure()
        signal.raise_signal(signal.SIGINT)
        assert wait_for(readable=(idle,), deadline=time.monotonic()).expired
        assert not operation.begin_closure()
        assert operation.requested


def test_closure_without_cancellation_does_not_report_cancellation() -> None:
    with operation_scope() as operation:
        assert not operation.begin_closure()


def test_nested_scope_borrows_the_owner_without_installing_capture() -> None:
    with pytest.raises(OperationCancelled), operation_scope() as outer:
        handler = signal.getsignal(signal.SIGINT)
        wakeup = installed_wakeup_fd()
        with operation_scope() as inner:
            assert inner is outer
            signal.raise_signal(signal.SIGINT)
        # The borrower neither restored capture nor projected the cancellation.
        assert signal.getsignal(signal.SIGINT) == handler
        assert installed_wakeup_fd() == wakeup
        assert current_operation() is outer
    assert current_operation() is None


def test_capture_is_installed_only_on_the_main_thread() -> None:
    previous = signal.getsignal(signal.SIGINT)
    observed: list[tuple[bool, object]] = []

    def scoped() -> None:
        with operation_scope() as operation:
            observed.append((operation.capturing, operation.native_wakeup_fd))

    worker = threading.Thread(target=scoped)
    worker.start()
    worker.join(GUARD)
    assert observed == [(False, None)]
    assert signal.getsignal(signal.SIGINT) is previous


@pytest.mark.parametrize(
    "disposition", (signal.SIG_IGN, signal.SIG_DFL), ids=("ignored", "default")
)
def test_native_sigint_dispositions_are_preserved_without_capture(disposition) -> None:
    previous = signal.signal(signal.SIGINT, disposition)
    try:
        with operation_scope() as operation:
            assert not operation.capturing
            assert signal.getsignal(signal.SIGINT) == disposition
    finally:
        signal.signal(signal.SIGINT, previous)


def test_failed_wakeup_restoration_keeps_the_descriptor_open(monkeypatch) -> None:
    previous_handler = signal.getsignal(signal.SIGINT)
    restore = signal.set_wakeup_fd
    failure = OSError("wakeup restoration failed")
    retained: list[int] = []

    def fail_restoration(descriptor: int, **kwargs: bool) -> int:
        if descriptor == -1:
            retained.append(restore(-1))
            restore(retained[0], warn_on_full_buffer=False)
            raise failure
        return restore(descriptor, **kwargs)

    with pytest.raises(OSError) as raised, operation_scope():
        monkeypatch.setattr(signal, "set_wakeup_fd", fail_restoration)
    monkeypatch.undo()
    try:
        assert raised.value is failure
        # CPython still references the descriptor, so it was not released.
        os.fstat(retained[0])
        # Independent restoration still returned SIGINT to its previous owner.
        assert signal.getsignal(signal.SIGINT) is previous_handler
    finally:
        signal.set_wakeup_fd(-1)
        os.close(retained[0])


def test_scope_failure_keeps_primary_and_records_cancellation() -> None:
    primary = RuntimeError("primary operation failure")
    with pytest.raises(RuntimeError) as raised, operation_scope():
        signal.raise_signal(signal.SIGINT)
        raise primary
    assert raised.value is primary
    assert any("cancellation" in note for note in raised.value.__notes__)


def test_interactive_interval_owns_sigint_and_restores_capture() -> None:
    with operation_scope() as operation:
        with operation.interactive_client():
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
            # Ignored native delivery belongs to the client interval.
            signal.raise_signal(signal.SIGINT)
        assert not operation.requested
    with pytest.raises(OperationCancelled), operation_scope() as operation:
        with operation.interactive_client():
            pass
        # Restored runner capture latches instead of raising at delivery.
        signal.raise_signal(signal.SIGINT)
        assert operation.requested


def test_interactive_entry_refuses_a_cancellation_latched_before_transition() -> None:
    entered = False
    with pytest.raises(OperationCancelled), operation_scope() as operation:
        signal.raise_signal(signal.SIGINT)
        try:
            with operation.interactive_client():
                entered = True
        finally:
            assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN
            signal.raise_signal(signal.SIGINT)
    assert not entered


def test_client_launch_failure_restores_runner_capture() -> None:
    failure = FileNotFoundError("configured GDB wrapper not found")
    with pytest.raises(OperationCancelled), operation_scope() as operation:
        with pytest.raises(FileNotFoundError) as raised, operation.interactive_client():
            raise failure
        assert raised.value is failure
        assert not operation.requested
        signal.raise_signal(signal.SIGINT)
        assert operation.requested


def test_operation_cancellation_has_one_identity() -> None:
    operation = OperationCancellation()
    assert operation.cancellation is operation.cancellation
    assert not isinstance(operation.cancellation, Exception)
    assert current_operation() is None
