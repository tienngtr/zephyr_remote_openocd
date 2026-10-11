# SPDX-License-Identifier: Apache-2.0

"""Eligibility at each local launch boundary, including queued dispatch."""

import asyncio

import pytest
from zephyr_remote_openocd.remote.launch import LaunchDenied, LaunchGate
from zephyr_remote_openocd.remote.outcome import Diagnostic

from tests.forwarding_support import GDB, RTT


def test_ready_and_forwarding_are_both_required_at_entry() -> None:
    gate = LaunchGate()
    ticket = gate.prepare((GDB,))
    assert not gate.enter(ticket)
    gate.observe_remote_ready()
    assert not gate.enter(ticket)
    gate.observe_forwarded((GDB,))
    assert gate.enter(ticket)
    assert not gate.enter(ticket)


@pytest.mark.parametrize("fatal", (False, True), ids=("cancel", "fatal-failure"))
def test_late_ready_cannot_restore_cancelled_eligibility(fatal: bool) -> None:
    gate = LaunchGate()
    ticket = gate.prepare((GDB,))
    gate.observe_forwarded((GDB,))
    first_failure = Diagnostic("TRANSPORT", "transport failed")
    if fatal:
        gate.fail(first_failure)
        gate.fail(Diagnostic("CLEANUP", "cleanup also failed"))
        assert gate.failure == first_failure
    else:
        gate.cancel()
    gate.observe_remote_ready()
    assert not gate.enter(ticket)
    with pytest.raises(LaunchDenied):
        gate.prepare((RTT,))


def test_next_boundary_requires_its_forwards_and_rejects_old_launch_token() -> None:
    gate = LaunchGate()
    gate.observe_remote_ready()
    gate.observe_forwarded((GDB,))
    gdb_ticket = gate.prepare((GDB,))
    assert gate.enter(gdb_ticket)
    rtt_ticket = gate.prepare((RTT,))
    assert not gate.enter(gdb_ticket)
    assert not gate.enter(rtt_ticket)
    gate.observe_forwarded((GDB, RTT))
    assert gate.enter(rtt_ticket)
    gate.end()
    assert not gate.enter(rtt_ticket)
    with pytest.raises(LaunchDenied):
        gate.prepare((GDB,))


def test_queued_launch_checks_cancellation_at_actual_execution() -> None:
    async def scenario() -> None:
        gate = LaunchGate()
        gate.observe_remote_ready()
        gate.observe_forwarded((GDB,))
        ticket = gate.prepare((GDB,))
        executed: list[str] = []
        dispatched = asyncio.get_running_loop().create_future()

        def entry() -> None:
            if gate.enter(ticket):
                executed.append("GDB")
            dispatched.set_result(None)

        asyncio.get_running_loop().call_soon(entry)
        gate.cancel()
        await dispatched
        assert executed == []

    asyncio.run(scenario())
