# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import tempfile

import pytest
from zephyr_remote_openocd.remote import rtt as rtt_module


def test_run_rtt_client_receives_after_transient_would_block(monkeypatch):
    class Connection:
        def __init__(self):
            self.received = False
            self.closed = False

        def recv(self, _size):
            if not self.received:
                self.received = True
                raise BlockingIOError
            return b"RTT output"

        def close(self):
            self.closed = True

    connection = Connection()
    poll_results = iter((None, None, 0))
    select_calls = []

    def select(readers, writers, errors, timeout):
        select_calls.append((readers, writers, errors, timeout))
        return [connection], [], []

    output = io.BytesIO()
    monkeypatch.setattr(rtt_module, "_connect", lambda _port, _timeout: (connection, b""))
    monkeypatch.setattr(rtt_module.select, "select", select)
    monkeypatch.setattr(rtt_module.os, "isatty", lambda _fd: False)

    with tempfile.TemporaryFile("w+b") as input_stream:
        result = rtt_module.run_rtt_client(
            5566,
            lambda: next(poll_results),
            stdin=input_stream,
            stdout=output,
        )

    assert result == 0
    assert output.getvalue() == b"RTT output"
    assert len(select_calls) == 2
    assert connection.closed


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
