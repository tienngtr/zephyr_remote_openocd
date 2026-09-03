# SPDX-License-Identifier: Apache-2.0

"""Lifecycle-aware local client for a forwarded OpenOCD RTT channel."""

from __future__ import annotations

import os
import select
import socket
import sys
import termios
import time
from collections.abc import Callable
from typing import BinaryIO


class RttClientError(RuntimeError):
    pass


def _connect(port: int, timeout: float) -> tuple[socket.socket, bytes]:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        channel_failed = False
        connection = None
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            connection.setblocking(False)
            readable, _, _ = select.select((connection,), (), (), 0.25)
            if readable:
                initial = connection.recv(4096)
                if not initial:
                    connection.close()
                    channel_failed = True
            else:
                initial = b""
        except OSError as error:
            if connection is not None:
                connection.close()
            last_error = error
            time.sleep(0.05)
            continue
        if channel_failed:
            raise RttClientError("RTT forward could not open the remote channel")
        connection.setblocking(True)
        return connection, initial
    raise RttClientError(f"cannot connect to local RTT port 127.0.0.1:{port}") from last_error


def run_rtt_client(
    port: int,
    poll_session: Callable[[], int | None],
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    startup_timeout: float = 5.0,
) -> int | None:
    """Relay channel 0 until either endpoint or the remote session exits."""
    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout.buffer
    connection, initial = _connect(port, startup_timeout)
    input_fd = input_stream.fileno()
    original_terminal = None
    try:
        if initial:
            output_stream.write(initial)
            output_stream.flush()
        if os.isatty(input_fd):
            original_terminal = termios.tcgetattr(input_fd)
            client_terminal = termios.tcgetattr(input_fd)
            client_terminal[3] &= ~(termios.ICANON | termios.ECHO)
            termios.tcsetattr(input_fd, termios.TCSAFLUSH, client_terminal)

        input_open = True
        while True:
            returncode = poll_session()
            if returncode is not None:
                return returncode
            inputs = (input_fd, connection) if input_open else (connection,)
            readable, _, _ = select.select(inputs, (), (), 0.1)
            if input_open and input_fd in readable:
                payload = os.read(input_fd, 4096)
                if not payload:
                    input_open = False
                else:
                    connection.sendall(payload)
            if connection in readable:
                payload = connection.recv(4096)
                if not payload:
                    return poll_session()
                output_stream.write(payload)
                output_stream.flush()
    finally:
        connection.close()
        if original_terminal is not None:
            termios.tcsetattr(input_fd, termios.TCSAFLUSH, original_terminal)
