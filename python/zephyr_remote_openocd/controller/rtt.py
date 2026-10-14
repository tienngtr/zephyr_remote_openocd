# SPDX-License-Identifier: Apache-2.0

"""Lifecycle-aware local client for a forwarded OpenOCD RTT channel."""

from __future__ import annotations

import errno
import io
import os
import select
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
                # A failed attempt is retryable only once its socket is released.
                # Otherwise the attempt failure, carrying the cleanup failure,
                # is the operation's fatal outcome, as with forwarding rollback.
                try:
                    self.close()
                except BaseException as cleanup_error:
                    _add_failure_note(error, "RTT socket cleanup failed", cleanup_error)
                    raise error from None
                last_error = error
            # The retry delay passes through the common wait even when the
            # startup deadline has expired, so a latched cancellation wins.
            wait_for(deadline=min(deadline, time.monotonic() + _CONNECT_RETRY_DELAY))
            if time.monotonic() >= deadline:
                break
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
    """Own pending input and output and interpret readiness for an owned socket.

    Output for a descriptor-backed stream is written to the descriptor only
    once it reports writable, at most ``PIPE_BUF`` bytes at a time, which a
    writable pipe or socket accepts without blocking. The stream's buffer and
    its lock are not used, so neither a stalled output consumer nor another
    writer blocked on the same stream can hold the relay outside its wait. A
    terminal write may still wait briefly for the terminal.
    """

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
        self.pending_output = bytearray()
        try:
            self.output_fd: int | None = output_stream.fileno()
        except io.UnsupportedOperation:
            # An in-memory stream reports no readiness and is written directly.
            self.output_fd = None

    def run(self, initial: bytes = b"") -> int:
        """Relay ready, nonblocking I/O until the session reports a result."""
        channel = self.connection.fileno()
        self._queue_output(initial)
        while True:
            returncode = self.session.poll_operation()
            if returncode is not None:
                self._finish_output()
                return returncode
            input_readable = self.input_open and len(self.pending_input) < _MAX_PENDING_INPUT
            # The channel is read again only once earlier output is delivered.
            output_readable = not self.pending_output
            ready = wait_for(
                readable=(
                    *((self.input_fd,) if input_readable else ()),
                    *((channel,) if output_readable else ()),
                ),
                writable=(
                    *((channel,) if self.pending_input else ()),
                    *(
                        (self.output_fd,)
                        if self.pending_output and self.output_fd is not None
                        else ()
                    ),
                ),
                sources=self.session.observation_sources(),
            )
            if input_readable and self.input_fd in ready.readable:
                self._read_input()
            if channel in ready.writable and self.pending_input:
                self._send_pending_input()
            if self.output_fd in ready.writable and self.pending_output:
                self._deliver_output()
            if output_readable and channel in ready.readable:
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
        self._queue_output(payload)
        return None

    def _queue_output(self, payload: bytes) -> None:
        if self.output_fd is not None:
            self.pending_output.extend(payload)
        elif payload:
            self.output_stream.write(payload)
            self.output_stream.flush()

    def _finish_output(self) -> None:
        """Give output already accepted from the channel one delivery chance.

        At most one received chunk is pending. It is written if the output is
        writable now; the session result does not wait for a consumer that
        cannot take it, and such output is discarded like unread channel data.
        """
        if not self.pending_output:
            return
        assert self.output_fd is not None
        ready = wait_for(writable=(self.output_fd,), deadline=time.monotonic())
        if self.output_fd in ready.writable:
            self._deliver_output()

    def _deliver_output(self) -> None:
        """Retain undelivered bytes across partial writes and transient backpressure."""
        assert self.output_fd is not None
        try:
            written = os.write(self.output_fd, self.pending_output[: select.PIPE_BUF])
        except BlockingIOError:
            return
        del self.pending_output[:written]


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
        relay = _RttRelay(connection.socket, input_fd, output_stream, session)
        with _terminal_mode(input_fd):
            return relay.run(connection.initial)
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
