# SPDX-License-Identifier: Apache-2.0

"""Recorded helper failures interrupt START pipe backpressure."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterable
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller import helper_connection as connection_module
from zephyr_remote_openocd.controller.authority import SessionError
from zephyr_remote_openocd.controller.deployment import DeploymentResult
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.ssh import SshCommand
from zephyr_remote_openocd.controller.waiting import Descriptor, WaitResult, WaitSources, wait_for
from zephyr_remote_openocd.protocol.outcome import Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import ProtocolError, encode_frame

from tests.control_support import CREATED
from tests.protocol_support import terminal_message
from tests.session_support import NotifiedAuthority

pytestmark = pytest.mark.local
_NO_WAIT_SOURCES = WaitSources()


@pytest.mark.timeout(60)
@pytest.mark.parametrize("ending", ("reader", "helper", "clean-exit", "controller-eof"))
def test_recorded_session_end_stops_start_without_helper_consuming_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    notified_authority: NotifiedAuthority,
    ending: str,
) -> None:
    gate = tmp_path / "failure-gate"
    os.mkfifo(gate)
    if ending == "reader":
        event = b"invalid helper frame\n"
    elif ending == "helper":
        event = terminal_message(Trigger.HELPER_FAILURE, code="FAILED", message="helper failed")
    elif ending == "clean-exit":
        event = encode_frame("ATTEMPT", argv=["child"]) + terminal_message(
            Trigger.CHILD_EXIT, returncode=0
        )
    else:
        event = terminal_message(Trigger.CONTROLLER_EOF)
    # Keep stdin open and unread after reporting session end. Only lease closure
    # releases the process, so writability cannot wake a blocked START write.
    code = (
        "import os, select\n"
        f"os.write(1, {CREATED!r})\n"
        f"with open({str(gate)!r}, 'rb', buffering=0) as gate:\n"
        "    assert gate.read(1) == b'x'\n"
        f"os.write(1, {event!r})\n"
        "poller = select.poll()\n"
        "poller.register(0, select.POLLHUP)\n"
        "poller.poll()\n"
    )
    client = HelperConnection(
        SshCommand((sys.executable, "-c", code)),
        "host",
        DeploymentResult("/helper.py", "digest", False),
        authority=notified_authority.authority,
        notifier=notified_authority.notifier,
    )
    real_wait = connection_module.wait_for
    blocked = False
    failure_wakeup = False
    writes_after_terminal: list[int] = []
    real_write = os.write
    start_descriptor: int | None = None

    def observe_start_write(descriptor: int, data: bytes | memoryview) -> int:
        if (
            descriptor == start_descriptor
            and notified_authority.authority.snapshot().terminal is not None
        ):
            writes_after_terminal.append(len(data))
        return real_write(descriptor, data)

    def release_failure_on_backpressure(
        *,
        readable: Iterable[Descriptor] = (),
        writable: Iterable[Descriptor] = (),
        sources: WaitSources = _NO_WAIT_SOURCES,
        deadline: float | None = None,
    ) -> WaitResult:
        nonlocal blocked, failure_wakeup
        writable = tuple(writable)
        if writable and not blocked:
            blocked = True
            with gate.open("wb", buffering=0) as writer:
                writer.write(b"x")
        result = real_wait(readable=readable, writable=writable, sources=sources, deadline=deadline)
        if writable:
            failure_wakeup = failure_wakeup or result.notified
        return result

    try:
        client.acquire()
        process = client._process
        assert process is not None and process.stdin is not None
        start_descriptor = process.stdin.fileno()
        # Remove the acquisition hint so only the new failure wakes the write.
        notified_authority.notifier.drain()
        monkeypatch.setattr(connection_module, "wait_for", release_failure_on_backpressure)
        monkeypatch.setattr(connection_module.os, "write", observe_start_write)
        with pytest.raises(SessionError) as raised:
            client.start_process(ProcessSpec(("child", "x" * 512 * 1024)), ())
        assert blocked
        assert failure_wakeup
        assert not writes_after_terminal
        assert raised.value is notified_authority.authority.snapshot().failure
        if ending == "reader":
            assert isinstance(raised.value.__cause__, ProtocolError)
        else:
            assert notified_authority.authority.snapshot().terminal is not None
    finally:
        client.close()

    process = client._process
    assert process is not None and process.returncode == 0
    assert process.stdin is not None and process.stdin.closed
    assert process.stdout is not None and process.stdout.closed
    assert client._join_reader(0)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("ending", ("clean-exit", "helper-failure"))
def test_start_waits_for_pending_control_final_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    notified_authority: NotifiedAuthority,
    ending: str,
) -> None:
    gate = tmp_path / "exit-gate"
    os.mkfifo(gate)
    output_entered = threading.Event()
    release_output = threading.Event()
    reader_waiting = threading.Event()
    pending_waits: list[tuple[Descriptor, ...]] = []
    late_writes: list[int] = []
    terminal = (
        terminal_message(Trigger.CHILD_EXIT, returncode=0)
        if ending == "clean-exit"
        else terminal_message(
            Trigger.HELPER_FAILURE,
            code="DETAIL",
            message="final helper failure",
            child_acquired=True,
        )
    )
    final = (
        encode_frame("ATTEMPT", argv=["child"])
        + encode_frame("CHILD_OUTPUT", stream="stdout", payload="final output", line_end=True)
        + terminal
    )
    code = (
        "import os\n"
        f"os.write(1, {CREATED!r})\n"
        f"with open({str(gate)!r}, 'rb', buffering=0) as gate:\n"
        "    assert gate.read(1) == b'x'\n"
        f"os.write(1, {final!r})\n"
    )

    def block_final_output(_stream: str, _payload: str, _line_end: bool) -> None:
        output_entered.set()
        assert release_output.wait(30), "final observation was not released"

    client = HelperConnection(
        SshCommand((sys.executable, "-c", code)),
        "host",
        DeploymentResult("/helper.py", "digest", False),
        output_handler=block_final_output,
        authority=notified_authority.authority,
        notifier=notified_authority.notifier,
    )
    blocked = False
    real_write = os.write
    start_descriptor: int | None = None

    def observe_start_write(descriptor: int, data: bytes | memoryview) -> int:
        snapshot = notified_authority.authority.snapshot()
        if descriptor == start_descriptor and (
            snapshot.control_ended or snapshot.terminal is not None
        ):
            late_writes.append(len(data))
        return real_write(descriptor, data)

    def coordinate_final_observation(
        *,
        readable: Iterable[Descriptor] = (),
        writable: Iterable[Descriptor] = (),
        sources: WaitSources = _NO_WAIT_SOURCES,
        deadline: float | None = None,
    ) -> WaitResult:
        nonlocal blocked
        writable = tuple(writable)
        snapshot = notified_authority.authority.snapshot()
        if sources.processes and snapshot.allocation is not None:
            # Leave the final bytes to the real exit scan rather than ordinary
            # readability, and establish this boundary before releasing exit.
            reader_waiting.set()
            return wait_for(sources=sources)
        if writable and not blocked:
            blocked = True
            assert reader_waiting.wait(30), "reader did not enter its exit wait"
            with gate.open("wb", buffering=0) as writer:
                writer.write(b"x")
        if snapshot.control_observation_pending and not sources.processes:
            assert output_entered.wait(30), "final output was not observed"
            assert snapshot.failure is None
            pending_waits.append(writable)
            # Only a coordinator that defers its write can release the final
            # callback and discover the terminal detail behind it.
            release_output.set()
        result = wait_for(readable=readable, writable=writable, sources=sources, deadline=deadline)
        if writable:
            # Process exit also makes stdin writable (HUP). Arrange recognition
            # of the durable control-end fact before resuming the START owner.
            assert output_entered.wait(30), "control exit was not recognized"
        return result

    monkeypatch.setattr(connection_module, "wait_for", coordinate_final_observation)
    monkeypatch.setattr(connection_module.os, "write", observe_start_write)
    try:
        client.acquire()
        process = client._process
        assert process is not None and process.stdin is not None
        start_descriptor = process.stdin.fileno()
        notified_authority.notifier.drain()
        with pytest.raises(SessionError) as raised:
            client.start_process(ProcessSpec(("child", "x" * 512 * 1024)), ())
        snapshot = notified_authority.authority.snapshot()
        assert blocked
        assert pending_waits and all(not descriptors for descriptors in pending_waits)
        assert not late_writes
        assert snapshot.terminal is not None
        assert snapshot.failure is raised.value
        if ending == "helper-failure":
            assert snapshot.terminal.outcome.primary_failure is not None
            assert snapshot.diagnostics[0].code == "TERMINAL_FAILURE"
        else:
            assert not snapshot.control_observation_pending
            assert snapshot.terminal.outcome.primary_failure is None
            assert snapshot.diagnostics[0].code == "OPERATION_FAILURE"
    finally:
        release_output.set()
        client.close()

    process = client._process
    assert process is not None and process.returncode == 0
    assert process.stdin is not None and process.stdin.closed
    assert process.stdout is not None and process.stdout.closed
    assert client._join_reader(0)
