# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import socket
import tempfile
from collections.abc import Iterator
from unittest.mock import MagicMock, create_autospec

import pytest
from zephyr_remote_openocd.remote import rtt as rtt_module


@pytest.fixture
def connection_boundary(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Exercise real connection setup with only socket/readiness I/O replaced."""
    connection = create_autospec(socket.socket, instance=True, spec_set=True)
    connection.recv.side_effect = AssertionError("unexpected socket read")
    connection.send.side_effect = AssertionError("unexpected socket write")
    monkeypatch.setattr(
        rtt_module.socket,
        "create_connection",
        create_autospec(socket.create_connection, return_value=connection),
    )
    monkeypatch.setattr(
        rtt_module.select,
        "select",
        create_autospec(rtt_module.select.select, return_value=([], [], [])),
    )
    return connection


def test_run_rtt_client_receives_after_transient_would_block(
    monkeypatch: pytest.MonkeyPatch, connection_boundary: MagicMock
) -> None:
    payloads = iter((BlockingIOError(), b"RTT output"))

    def receive(_size: int) -> bytes:
        payload = next(payloads)
        if isinstance(payload, BlockingIOError):
            raise payload
        return payload

    connection_boundary.recv.side_effect = receive
    readiness: Iterator[tuple[list[MagicMock], list[object], list[object]]] = iter(
        (([], [], []), ([connection_boundary], [], []), ([connection_boundary], [], []))
    )
    monkeypatch.setattr(rtt_module.select, "select", lambda *_args: next(readiness))
    with tempfile.TemporaryFile("w+b") as stdin, io.BytesIO() as stdout:
        assert (
            rtt_module.run_rtt_client(
                5566,
                lambda: 0 if stdout.getvalue() == b"RTT output" else None,
                stdin=stdin,
                stdout=stdout,
            )
            == 0
        )
        assert stdout.getvalue() == b"RTT output"
    connection_boundary.close.assert_called_once()


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


def test_connection_closes_when_stdin_has_no_descriptor(connection_boundary: MagicMock) -> None:
    with (
        io.BytesIO() as stdin,
        io.BytesIO() as stdout,
        pytest.raises(io.UnsupportedOperation),
    ):
        rtt_module.run_rtt_client(5566, lambda: None, stdin=stdin, stdout=stdout)
    connection_boundary.close.assert_called_once()


def test_pending_input_survives_would_block_and_partial_send(
    monkeypatch: pytest.MonkeyPatch, connection_boundary: MagicMock
) -> None:
    request = b"local input"
    attempts: list[bytes] = []
    forwarded = bytearray()
    results = iter((BlockingIOError(), 2, len(request) - 2))

    def send(payload: bytearray) -> int:
        attempts.append(bytes(payload))
        result = next(results)
        if isinstance(result, BlockingIOError):
            raise result
        forwarded.extend(payload[:result])
        return result

    connection_boundary.send.side_effect = send
    with tempfile.TemporaryFile("w+b") as stdin, io.BytesIO() as stdout:
        stdin.write(request)
        stdin.seek(0)
        readiness: Iterator[tuple[list[int], list[MagicMock], list[object]]] = iter(
            (
                ([], [], []),  # Connection setup sees an open, idle channel.
                ([stdin.fileno()], [], []),
                ([], [connection_boundary], []),
                ([], [connection_boundary], []),
                ([], [connection_boundary], []),
            )
        )
        monkeypatch.setattr(rtt_module.select, "select", lambda *_args: next(readiness))
        result = rtt_module.run_rtt_client(
            5566,
            lambda: 0 if forwarded == request else None,
            stdin=stdin,
            stdout=stdout,
        )

    assert result == 0
    assert attempts == [request, request, request[2:]]
    assert forwarded == request
    connection_boundary.close.assert_called_once()


def test_zero_byte_send_reports_channel_failure(
    monkeypatch: pytest.MonkeyPatch, connection_boundary: MagicMock
) -> None:
    connection_boundary.send.side_effect = None
    connection_boundary.send.return_value = 0
    with tempfile.TemporaryFile("w+b") as stdin, io.BytesIO() as stdout:
        stdin.write(b"input")
        stdin.seek(0)
        readiness: Iterator[tuple[list[int], list[MagicMock], list[object]]] = iter(
            (
                ([], [], []),
                ([stdin.fileno()], [], []),
                ([], [connection_boundary], []),
            )
        )
        monkeypatch.setattr(rtt_module.select, "select", lambda *_args: next(readiness))
        with pytest.raises(rtt_module.RttClientError):
            rtt_module.run_rtt_client(5566, lambda: None, stdin=stdin, stdout=stdout)
    connection_boundary.close.assert_called_once()


@pytest.mark.parametrize("phase", ("terminal-setup", "relay"))
@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_terminal_and_socket_cleanup_preserve_primary_failure(
    monkeypatch: pytest.MonkeyPatch,
    connection_boundary: MagicMock,
    phase: str,
    failure_type: type[BaseException],
) -> None:
    failure = failure_type("primary failure")
    restore_failure = OSError("terminal cleanup failure")
    close_failure = OSError("socket cleanup failure")
    original = [1, 2, 3, rtt_module.termios.ICANON | rtt_module.termios.ECHO, 5, 6, [7]]
    restored = []

    def set_attributes(_fd: int, _when: int, attributes: list) -> None:
        if attributes == original:
            restored.append(attributes)
            raise restore_failure
        if phase == "terminal-setup":
            raise failure

    def poll_session() -> int | None:
        raise failure

    monkeypatch.setattr(rtt_module.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(rtt_module.termios, "tcgetattr", lambda _fd: list(original))
    monkeypatch.setattr(rtt_module.termios, "tcsetattr", set_attributes)
    connection_boundary.close.side_effect = close_failure
    with (
        tempfile.TemporaryFile("w+b") as stdin,
        io.BytesIO() as stdout,
        pytest.raises(failure_type) as raised,
    ):
        rtt_module.run_rtt_client(5566, poll_session, stdin=stdin, stdout=stdout)

    assert raised.value is failure
    assert restored == [original]
    notes = "\n".join(raised.value.__notes__)
    assert str(restore_failure) in notes
    assert str(close_failure) in notes
    connection_boundary.close.assert_called_once()


@pytest.mark.parametrize("cleanup", ("terminal", "socket"))
def test_cleanup_failure_is_reported_after_session_exit(
    monkeypatch: pytest.MonkeyPatch, connection_boundary: MagicMock, cleanup: str
) -> None:
    failure = OSError("cleanup failure")
    original = [1, 2, 3, rtt_module.termios.ICANON | rtt_module.termios.ECHO, 5, 6, [7]]

    def set_attributes(_fd: int, _when: int, attributes: list) -> None:
        if attributes == original and cleanup == "terminal":
            raise failure

    monkeypatch.setattr(rtt_module.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(rtt_module.termios, "tcgetattr", lambda _fd: list(original))
    monkeypatch.setattr(rtt_module.termios, "tcsetattr", set_attributes)
    if cleanup == "socket":
        connection_boundary.close.side_effect = failure
    with (
        tempfile.TemporaryFile("w+b") as stdin,
        io.BytesIO() as stdout,
        pytest.raises(OSError) as raised,
    ):
        rtt_module.run_rtt_client(5566, lambda: 0, stdin=stdin, stdout=stdout)

    assert raised.value is failure
    connection_boundary.close.assert_called_once()
