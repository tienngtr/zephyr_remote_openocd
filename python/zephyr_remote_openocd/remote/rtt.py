# SPDX-License-Identifier: Apache-2.0

"""Lifecycle-aware local client for a forwarded OpenOCD RTT channel."""

from __future__ import annotations

import os
import select
import socket
import sys
import termios
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import BinaryIO

from .cleanup import _add_failure_note


class RttClientError(RuntimeError):
    pass


_INPUT_CHUNK_SIZE = 4096
_MAX_PENDING_INPUT = 64 * 1024


def _connect(port: int, timeout: float) -> tuple[socket.socket, bytes]:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        connection = None
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            connection.setblocking(False)
            readable, _, _ = select.select((connection,), (), (), 0.25)
            if readable:
                initial = connection.recv(4096)
                if not initial:
                    raise RttClientError("RTT forward could not open the remote channel")
            else:
                initial = b""
            return connection, initial
        except BaseException as error:
            cleanup_failed = False
            if connection is not None:
                try:
                    connection.close()
                except BaseException as cleanup_error:
                    _add_failure_note(error, "RTT socket cleanup failed", cleanup_error)
                    cleanup_failed = True
            if cleanup_failed or not isinstance(error, OSError):
                raise
            last_error = error
            time.sleep(0.05)
            continue
    raise RttClientError(f"cannot connect to local RTT port 127.0.0.1:{port}") from last_error


@contextmanager
def _terminal_mode(input_fd: int) -> Iterator[None]:
    """Own noncanonical/no-echo input and restore it even if setup fails."""
    original_terminal = None
    primary_failure: BaseException | None = None
    try:
        if os.isatty(input_fd):
            original_terminal = termios.tcgetattr(input_fd)
            client_terminal = termios.tcgetattr(input_fd)
            client_terminal[3] &= ~(termios.ICANON | termios.ECHO)
            termios.tcsetattr(input_fd, termios.TCSAFLUSH, client_terminal)
        yield
    except BaseException as error:
        primary_failure = error
        raise
    finally:
        if original_terminal is not None:
            try:
                termios.tcsetattr(input_fd, termios.TCSAFLUSH, original_terminal)
            except BaseException as error:
                if primary_failure is None:
                    raise
                _add_failure_note(primary_failure, "RTT terminal restoration failed", error)


class _RttRelay:
    """Own pending input and interpret readiness for an already-owned socket."""

    def __init__(
        self,
        connection: socket.socket,
        input_fd: int,
        output_stream: BinaryIO,
        poll_session: Callable[[], int | None],
    ) -> None:
        self.connection = connection
        self.input_fd = input_fd
        self.output_stream = output_stream
        self.poll_session = poll_session
        self.input_open = True
        self.pending_input = bytearray()

    def run(self) -> int:
        """Poll session liveness while relaying only ready, nonblocking I/O."""
        while True:
            returncode = self.poll_session()
            if returncode is not None:
                return returncode
            input_readable = self.input_open and len(self.pending_input) < _MAX_PENDING_INPUT
            inputs: tuple[int | socket.socket, ...] = (
                (self.input_fd, self.connection) if input_readable else (self.connection,)
            )
            writable_inputs = (self.connection,) if self.pending_input else ()
            readable, writable, _ = select.select(inputs, writable_inputs, (), 0.1)
            if input_readable and self.input_fd in readable:
                self._read_input()
            if self.connection in writable and self.pending_input:
                self._send_pending_input()
            if self.connection in readable:
                returncode = self._receive_output()
                if returncode is not None:
                    return returncode

    def _read_input(self) -> None:
        """Stop selecting stdin on EOF or while the bounded queue is full."""
        payload = os.read(
            self.input_fd,
            min(_INPUT_CHUNK_SIZE, _MAX_PENDING_INPUT - len(self.pending_input)),
        )
        if not payload:
            self.input_open = False
        else:
            self.pending_input.extend(payload)

    def _send_pending_input(self) -> None:
        """Retain unsent bytes across partial writes and transient backpressure."""
        try:
            sent = self.connection.send(self.pending_input)
        except BlockingIOError:
            return
        if sent <= 0:
            raise RttClientError("RTT channel closed while sending input")
        del self.pending_input[:sent]

    def _receive_output(self) -> int | None:
        """Give a recorded session exit precedence over receive-side channel EOF."""
        try:
            payload = self.connection.recv(_INPUT_CHUNK_SIZE)
        except BlockingIOError:
            return None
        if not payload:
            returncode = self.poll_session()
            if returncode is not None:
                return returncode
            raise RttClientError("RTT channel closed while remote session is still running")
        self.output_stream.write(payload)
        self.output_stream.flush()
        return None


def run_rtt_client(
    port: int,
    poll_session: Callable[[], int | None],
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    startup_timeout: float = 5.0,
) -> int | None:
    """Own the RTT socket and terminal scope until the relay or session exits."""
    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout.buffer
    connection: socket.socket | None = None
    primary_failure: BaseException | None = None
    try:
        connection, initial = _connect(port, startup_timeout)
        input_fd = input_stream.fileno()
        if initial:
            output_stream.write(initial)
            output_stream.flush()
        with _terminal_mode(input_fd):
            return _RttRelay(connection, input_fd, output_stream, poll_session).run()
    except BaseException as error:
        primary_failure = error
        raise
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException as error:
                if primary_failure is None:
                    raise
                _add_failure_note(primary_failure, "RTT socket cleanup failed", error)
