# SPDX-License-Identifier: Apache-2.0

"""Lifecycle-aware local client for a forwarded OpenOCD RTT channel."""

from __future__ import annotations

import errno
import os
import socket
import sys
import termios
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import BinaryIO, Protocol

from zephyr_remote_openocd.controller.cleanup import _add_failure_note
from zephyr_remote_openocd.controller.waiting import WaitSources, wait_for


class RttClientError(RuntimeError):
    pass


class ObservedSession(Protocol):
    """Session status and wake sources an RTT client observes while relaying."""

    def poll_operation(self) -> int | None: ...

    def observation_sources(self) -> WaitSources: ...


_INPUT_CHUNK_SIZE = 4096
_MAX_PENDING_INPUT = 64 * 1024
# Real connection semantics: delay between refused attempts on the local
# forward, and how long an opened channel may take to report remote refusal.
_CONNECT_RETRY_DELAY = 0.05
_INITIAL_OUTPUT_GRACE = 0.25


class _RttConnection:
    """Acquire into an already-owned scope, without returning a live socket."""

    def __init__(self) -> None:
        self.socket: socket.socket | None = None
        self.initial = b""

    def acquire(self, port: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_error: OSError | None = None
        while True:
            try:
                self._attempt(port, deadline)
                return
            except OSError as error:
                try:
                    self.close()
                except BaseException as cleanup_error:
                    _add_failure_note(error, "RTT socket cleanup failed", cleanup_error)
                    raise
                last_error = error
            if time.monotonic() >= deadline:
                break
            wait_for(deadline=min(deadline, time.monotonic() + _CONNECT_RETRY_DELAY))
        raise RttClientError(f"cannot connect to local RTT port 127.0.0.1:{port}") from last_error

    def _attempt(self, port: int, deadline: float) -> None:
        # Register the socket on this already-owned connection as it is created.
        self.socket = socket.socket()
        connection = self.socket
        connection.setblocking(False)
        status = connection.connect_ex(("127.0.0.1", port))
        if status in (errno.EINPROGRESS, errno.EAGAIN):
            ready = wait_for(writable=(connection,), deadline=deadline)
            if connection.fileno() not in ready.writable:
                raise TimeoutError(errno.ETIMEDOUT, "RTT forward connection timed out")
            status = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if status:
            raise OSError(status, os.strerror(status))
        ready = wait_for(readable=(connection,), deadline=time.monotonic() + _INITIAL_OUTPUT_GRACE)
        if connection.fileno() in ready.readable:
            self.initial = connection.recv(4096)
            if not self.initial:
                raise RttClientError("RTT forward could not open the remote channel")

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None


@contextmanager
def _terminal_mode(input_fd: int) -> Generator[None, None, None]:
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
        session: ObservedSession,
    ) -> None:
        self.connection = connection
        self.input_fd = input_fd
        self.output_stream = output_stream
        self.session = session
        self.input_open = True
        self.pending_input = bytearray()

    def run(self) -> int:
        """Relay ready, nonblocking I/O until the session reports a result."""
        channel = self.connection.fileno()
        while True:
            returncode = self.session.poll_operation()
            if returncode is not None:
                return returncode
            input_readable = self.input_open and len(self.pending_input) < _MAX_PENDING_INPUT
            ready = wait_for(
                readable=(self.input_fd, channel) if input_readable else (channel,),
                writable=(channel,) if self.pending_input else (),
                sources=self.session.observation_sources(),
            )
            if input_readable and self.input_fd in ready.readable:
                self._read_input()
            if channel in ready.writable and self.pending_input:
                self._send_pending_input()
            if channel in ready.readable:
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
            returncode = self.session.poll_operation()
            if returncode is not None:
                return returncode
            raise RttClientError("RTT channel closed while remote session is still running")
        self.output_stream.write(payload)
        self.output_stream.flush()
        return None


def run_rtt_client(
    port: int,
    session: ObservedSession,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    startup_timeout: float = 5.0,
) -> int | None:
    """Own the RTT socket and terminal scope until the relay or session exits."""
    input_stream = stdin or sys.stdin.buffer
    output_stream = stdout or sys.stdout.buffer
    connection = _RttConnection()
    primary_failure: BaseException | None = None
    try:
        connection.acquire(port, startup_timeout)
        assert connection.socket is not None
        input_fd = input_stream.fileno()
        if connection.initial:
            output_stream.write(connection.initial)
            output_stream.flush()
        with _terminal_mode(input_fd):
            return _RttRelay(connection.socket, input_fd, output_stream, session).run()
    except BaseException as error:
        primary_failure = error
        raise
    finally:
        try:
            connection.close()
        except BaseException as error:
            if primary_failure is None:
                raise
            _add_failure_note(primary_failure, "RTT socket cleanup failed", error)
