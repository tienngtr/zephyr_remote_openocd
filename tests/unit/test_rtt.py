# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from zephyr_remote_openocd.remote import rtt as rtt_module


def test_connect_retries_refusal_until_startup_error(monkeypatch):
    port = 5566
    clock = [0.0]
    attempts = []

    def refuse_connection(address, *, timeout):
        attempts.append((address, timeout))
        raise ConnectionRefusedError("connection refused")

    def advance_clock(delay):
        clock[0] += delay

    monkeypatch.setattr(rtt_module.socket, "create_connection", refuse_connection)
    monkeypatch.setattr(rtt_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rtt_module.time, "sleep", advance_clock)

    with pytest.raises(rtt_module.RttClientError) as raised:
        rtt_module._connect(port, timeout=5.0)

    assert len(attempts) > 1
    assert all(address == ("127.0.0.1", port) for address, _ in attempts)
    assert raised.value.__cause__ is not None
    assert isinstance(raised.value.__cause__, ConnectionRefusedError)
    assert str(port) in str(raised.value)
