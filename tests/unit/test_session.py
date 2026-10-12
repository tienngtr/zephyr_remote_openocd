# SPDX-License-Identifier: Apache-2.0

"""Shared local authority: READY cannot override cancellation or recorded fatal facts."""

import threading

import pytest
from zephyr_remote_openocd.remote.model import Service
from zephyr_remote_openocd.remote.outcome import Trigger
from zephyr_remote_openocd.remote.session import SessionError, _SessionObservations

from tests.protocol_support import terminal_snapshot


@pytest.mark.parametrize("fact", ("cancel", "reader", "terminal"))
def test_recorded_fact_prevents_dependent_entry_after_ready(fact):
    observations = _SessionObservations()
    if fact == "cancel":
        observations.cancel()
    elif fact == "reader":
        observations.record_reader_failure(RuntimeError("reader failed"))
    else:
        observations.record_terminal(terminal_snapshot(Trigger.HELPER_FAILURE, code="FAILED"))
    observations.record_ready()
    with pytest.raises(SessionError):
        observations.enter((), ())


def test_ready_requires_required_forwarding_at_entry():
    observations = _SessionObservations()
    service = Service("gdb", 3333, 3333)
    observations.record_ready()
    with pytest.raises(SessionError):
        observations.enter((service,), ())
    observations.enter((service,), (service,))


@pytest.mark.timeout(10)
def test_queued_launch_rechecks_fatal_fact_at_execution_entry():
    observations = _SessionObservations()
    observations.record_ready()
    scheduled = threading.Event()
    execute = threading.Event()
    errors = []
    launched = []

    def queued_launch():
        scheduled.set()
        assert execute.wait(5)
        try:
            observations.enter((), ())
            launched.append(True)
        except SessionError as error:
            errors.append(error)

    thread = threading.Thread(target=queued_launch)
    thread.start()
    assert scheduled.wait(5)
    observations.record_reader_failure(RuntimeError("fatal fact before execution"))
    execute.set()
    thread.join(5)
    assert not thread.is_alive()
    assert errors and not launched


def test_reported_terminal_failure_is_not_replayed_during_close():
    observations = _SessionObservations()
    observations.record_terminal(terminal_snapshot(Trigger.HELPER_FAILURE, code="FAILED"))
    failure = observations.snapshot().failure
    assert failure is not None
    assert observations.publish_failure() is failure
    assert observations.take_unreported_failure() is None


def test_failure_inspection_does_not_acknowledge_delivery():
    observations = _SessionObservations()
    observations.record_reader_failure(RuntimeError("reader failed"))
    failure = observations.snapshot().failure
    assert failure is not None
    assert observations.snapshot().failure is failure
    assert observations.take_unreported_failure() is failure
    assert observations.take_unreported_failure() is None


def test_repeated_reader_failure_retains_first_fact_without_reopening_delivery():
    observations = _SessionObservations()
    first = RuntimeError("first reader failure")
    observations.record_reader_failure(first)
    published = observations.publish_failure()
    assert published is not None and published.__cause__ is first

    observations.record_reader_failure(RuntimeError("later reader failure"))

    snapshot = observations.snapshot()
    assert snapshot.failure is published
    assert len(snapshot.diagnostics) == 1
    assert observations.take_unreported_failure() is None


def test_later_reader_detail_reopens_delivery_with_established_terminal_primary():
    observations = _SessionObservations()
    observations.record_terminal(terminal_snapshot(Trigger.STARTUP_FAILURE, code="STARTUP_FAILURE"))
    first = observations.publish_failure()
    assert first is not None
    reader = RuntimeError("trailing protocol corruption")
    reader.add_note("retained reader detail")
    observations.record_reader_failure(reader)

    combined = observations.take_unreported_failure()
    assert combined is not None and str(combined) == str(first)
    assert any("trailing protocol corruption" in note for note in combined.__notes__)
    assert any("retained reader detail" in note for note in combined.__notes__)
    assert observations.take_unreported_failure() is None


@pytest.mark.timeout(10)
def test_integrated_entry_accounts_reader_fatal_after_preceding_helper_check(monkeypatch):
    from zephyr_remote_openocd.remote.backend import RemoteSession
    from zephyr_remote_openocd.remote.deploy import DeploymentResult
    from zephyr_remote_openocd.remote.helper_client import _HelperClient
    from zephyr_remote_openocd.remote.model import RemoteProcess, RemoteSessionRequest
    from zephyr_remote_openocd.remote.ssh import SshCommand

    request = RemoteSessionRequest("host", SshCommand(), RemoteProcess(("openocd",)))
    deployment = DeploymentResult("/helper.py", "digest", False)
    session = RemoteSession(request, deployment)
    helper = _HelperClient(
        request.ssh_command, request.host, deployment, observations=session._observations
    )
    session._helper = helper
    session._observations.record_ready()
    observing = threading.Event()
    observed = threading.Event()
    launched = []

    def reader():
        assert observing.wait(5)
        helper._observations.record_reader_failure(RuntimeError("concurrent helper failure"))
        observed.set()

    thread = threading.Thread(target=reader)
    thread.start()

    enter = session._observations.enter

    def execution_entry(required, forwarded):
        observing.set()
        assert observed.wait(5)
        enter(required, forwarded)

    monkeypatch.setattr(session._observations, "enter", execution_entry)
    try:
        with pytest.raises(SessionError, match="concurrent helper failure"):
            session.run_dependent(lambda: launched.append(True))
        assert not launched
    finally:
        thread.join(5)
        session.close()
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "first, second",
    tuple(
        (first, second)
        for first in ("child", "terminal", "reader", "transport", "operation", "forward")
        for second in ("child", "terminal", "reader", "transport", "operation", "forward")
        if first != second and {first, second} != {"child", "terminal"}
    ),
)
def test_all_failure_domains_preserve_one_primary_across_publication(first, second):
    """Publish one failure, recognize another, then project status and enter."""
    observations = _SessionObservations()
    observations.record_ready()
    later = RuntimeError(f"{second} failure")
    later.add_note("nested secondary diagnostic")

    def record(source, error):
        if source == "child":
            observations.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=7))
        elif source == "terminal":
            observations.record_terminal(
                terminal_snapshot(Trigger.HELPER_FAILURE, code="HELPER_FAILURE")
            )
        elif source == "reader":
            observations.record_reader_failure(error)
        elif source == "transport":
            observations.record_control_status(17)
        else:
            observations.record_failure(source.upper(), error)

    record(first, RuntimeError(f"{first} failure"))
    with pytest.raises(RuntimeError) as initial:
        observations.operation_status()
    record(second, later)
    with pytest.raises(RuntimeError) as status:
        observations.operation_status()
    with pytest.raises(RuntimeError) as launch:
        observations.enter((), ())
    assert status.value is initial.value is launch.value
    snapshot = observations.snapshot()
    assert len(snapshot.diagnostics) == 2
    if second in ("reader", "operation", "forward"):
        assert any("nested secondary diagnostic" in note for note in status.value.__notes__)
    if "child" in (first, second):
        assert snapshot.ending is not None
        assert snapshot.ending.outcome.child_result is not None
        assert snapshot.ending.outcome.child_result.returncode == 7
    notes = tuple(status.value.__notes__)
    with pytest.raises(RuntimeError) as repeated:
        observations.operation_status()
    assert repeated.value is initial.value
    assert tuple(repeated.value.__notes__) == notes
