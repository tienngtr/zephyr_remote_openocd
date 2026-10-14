# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from types import FrameType, SimpleNamespace
from typing import Any, override

import pytest
from zephyr_remote_openocd.controller import rtt as rtt_module
from zephyr_remote_openocd.controller.waiting import WaitResult, WaitSources

from tests.session_support import ObservedStatus

GUARD = 30


class RttServer:
    """A loopback forward endpoint running one connection script."""

    def __init__(self, serve: Callable[[socket.socket], None]) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.errors: list[BaseException] = []
        self._accepted = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(serve,), daemon=True)
        self._thread.start()

    def _run(self, serve: Callable[[socket.socket], None]) -> None:
        try:
            with self.listener, self.listener.accept()[0] as connection:
                self._accepted.set()
                serve(connection)
        except BaseException as error:
            self.errors.append(error)

    def join(self) -> None:
        if not self._accepted.is_set():
            # The client never connected; complete the pending accept once.
            with socket.create_connection(("127.0.0.1", self.port)):
                pass
        self._thread.join(GUARD)
        assert not self._thread.is_alive(), "RTT test server did not stop"
        if self.errors:
            raise self.errors[0]


def idle(connection: socket.socket) -> None:
    """Keep the forwarded channel open until the client closes it."""
    while connection.recv(4096):
        pass


class TrackedChannel(socket.socket):
    """A real socket; subclasses override one operation to inject faults."""

    created: list[TrackedChannel] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.created.append(self)


@pytest.fixture
def channels(monkeypatch) -> Callable[[type[TrackedChannel]], type[TrackedChannel]]:
    def install(channel_type: type[TrackedChannel]) -> type[TrackedChannel]:
        channel_type.created = []
        # Replace only the RTT client's socket constructor, not the test server's.
        client_socket = SimpleNamespace(**vars(socket))
        client_socket.socket = channel_type
        monkeypatch.setattr(rtt_module, "socket", client_socket)
        return channel_type

    return install


pytestmark = pytest.mark.usefixtures("requires_loopback_listener")


def test_relay_delivers_output_and_returns_the_session_result(observed_session) -> None:
    def serve(connection: socket.socket) -> None:
        connection.sendall(b"RTT output")
        idle(connection)

    server = RttServer(serve)
    with tempfile.TemporaryFile("w+b") as stdin, io.BytesIO() as stdout:
        status = observed_session(lambda: 0 if stdout.getvalue() == b"RTT output" else None)
        assert rtt_module.run_rtt_client(server.port, status, stdin=stdin, stdout=stdout) == 0
    server.join()


def test_session_result_published_before_sleep_ends_an_idle_relay(observed_session) -> None:
    finished = threading.Event()

    class Status(ObservedStatus):
        @override
        def observation_sources(self) -> WaitSources:
            # The session publishes its result after the relay's last status
            # snapshot and before it sleeps; only the notification can wake it.
            finished.set()
            self.changed()
            return super().observation_sources()

    server = RttServer(idle)
    status = Status(lambda: 0 if finished.is_set() else None)
    try:
        with tempfile.TemporaryFile("w+b") as stdin, io.BytesIO() as stdout:
            assert rtt_module.run_rtt_client(server.port, status, stdin=stdin, stdout=stdout) == 0
    finally:
        status.close()
    server.join()


def test_receive_tolerates_spurious_readiness(observed_session) -> None:
    class SpuriousChannel(socket.socket):
        spurious = True

        @override
        def recv(self, bufsize: int, flags: int = 0) -> bytes:
            if self.spurious:
                self.spurious = False
                raise BlockingIOError
            return super().recv(bufsize, flags)

    local, peer = socket.socketpair()
    channel = SpuriousChannel(fileno=local.detach())
    channel.setblocking(False)
    with channel, peer, tempfile.TemporaryFile("w+b") as stdin, io.BytesIO() as stdout:
        peer.sendall(b"RTT output")
        status = observed_session(lambda: 0 if stdout.getvalue() == b"RTT output" else None)
        assert rtt_module._RttRelay(channel, stdin.fileno(), stdout, status).run() == 0


def test_connect_retries_refusal_until_startup_error(
    monkeypatch, channels, observed_session
) -> None:
    channel_type = channels(TrackedChannel)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]

    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()

    real_wait = rtt_module.wait_for

    def retry_delay(*, deadline: float, **kwargs: Any) -> WaitResult:
        if kwargs:
            # Connection completion is real socket readiness.
            return real_wait(deadline=None, **kwargs)
        # Retry delays elapse on the fake clock.
        clock.now = deadline
        return WaitResult(frozenset(), frozenset(), frozenset(), False, True)

    monkeypatch.setattr(rtt_module, "time", clock)
    monkeypatch.setattr(rtt_module, "wait_for", retry_delay)
    with (
        io.BytesIO() as stdin,
        io.BytesIO() as stdout,
        pytest.raises(rtt_module.RttClientError) as raised,
    ):
        rtt_module.run_rtt_client(port, observed_session(lambda: None), stdin=stdin, stdout=stdout)

    assert len(channel_type.created) > 1
    assert all(channel.fileno() == -1 for channel in channel_type.created)
    assert isinstance(raised.value.__cause__, ConnectionRefusedError)
    assert str(port) in str(raised.value)


def test_connection_closes_when_stdin_has_no_descriptor(channels, observed_session) -> None:
    channel_type = channels(TrackedChannel)
    server = RttServer(idle)
    with (
        io.BytesIO() as stdin,
        io.BytesIO() as stdout,
        pytest.raises(io.UnsupportedOperation),
    ):
        rtt_module.run_rtt_client(
            server.port, observed_session(lambda: None), stdin=stdin, stdout=stdout
        )
    (channel,) = channel_type.created
    assert channel.fileno() == -1
    server.join()


@pytest.mark.parametrize(
    "phase", ("connect", "initial-readiness", "acquire-return", "caller-resumption")
)
@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_interruption_at_acquisition_boundaries_closes_owned_socket(
    monkeypatch, channels, observed_session, phase, cleanup_fails
) -> None:
    cleanup_error = OSError("RTT acquisition socket cleanup failed")
    interruption = KeyboardInterrupt("RTT connection acquisition interrupted")
    interrupted = False

    def interrupt() -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise interruption

    class Channel(TrackedChannel):
        @override
        def connect_ex(self, address: Any) -> int:
            if phase == "connect":
                interrupt()
            return super().connect_ex(address)

        @override
        def close(self) -> None:
            super().close()
            if cleanup_fails:
                raise cleanup_error

    channel_type = channels(Channel)
    real_wait = rtt_module.wait_for

    def readiness(**kwargs: Any) -> WaitResult:
        if phase == "initial-readiness" and kwargs.get("readable"):
            interrupt()
        return real_wait(**kwargs)

    def interrupt_at_boundary(frame: FrameType, event: str, _arg: object) -> Any:
        if (
            phase == "acquire-return"
            and event == "return"
            and frame.f_code is rtt_module._RttConnection.acquire.__code__
        ):
            interrupt()
        owner = frame.f_locals.get("connection")
        if (
            phase == "caller-resumption"
            and event == "line"
            and frame.f_code is rtt_module.run_rtt_client.__code__
            and isinstance(owner, rtt_module._RttConnection)
            and owner.socket is not None
            and "input_fd" not in frame.f_locals
        ):
            interrupt()
        return interrupt_at_boundary

    monkeypatch.setattr(rtt_module, "wait_for", readiness)
    server = RttServer(idle)
    previous_trace = sys.gettrace()
    try:
        with io.BytesIO() as stdin, io.BytesIO() as stdout:
            if phase in ("acquire-return", "caller-resumption"):
                sys.settrace(interrupt_at_boundary)
            try:
                with pytest.raises(KeyboardInterrupt) as raised:
                    rtt_module.run_rtt_client(
                        server.port, observed_session(lambda: None), stdin=stdin, stdout=stdout
                    )
            finally:
                sys.settrace(previous_trace)
    finally:
        sys.settrace(previous_trace)
    assert interrupted
    assert raised.value is interruption
    (channel,) = channel_type.created
    assert channel.fileno() == -1
    if cleanup_fails:
        assert any(str(cleanup_error) in note for note in raised.value.__notes__)
    server.join()


def test_pending_input_survives_would_block_and_partial_send(observed_session) -> None:
    request = b"local input"
    attempts: list[bytes] = []
    received = bytearray()
    results: Iterator[int | None] = iter((None, 2))

    class BackpressureChannel(socket.socket):
        @override
        def send(self, data: Any, flags: int = 0) -> int:
            attempts.append(bytes(data))
            limit = next(results, len(data))
            if limit is None:
                raise BlockingIOError
            return super().send(bytes(data[:limit]), flags)

    local, peer = socket.socketpair()
    channel = BackpressureChannel(fileno=local.detach())
    channel.setblocking(False)
    with channel, peer, tempfile.TemporaryFile("w+b") as stdin, io.BytesIO() as stdout:
        stdin.write(request)
        stdin.seek(0)
        status = observed_session(lambda: 0 if received == request else None)

        def receive() -> None:
            while len(received) < len(request):
                received.extend(peer.recv(64))
            status.changed()

        receiver = threading.Thread(target=receive)
        receiver.start()
        try:
            assert rtt_module._RttRelay(channel, stdin.fileno(), stdout, status).run() == 0
        finally:
            receiver.join(GUARD)

    assert attempts == [request, request, request[2:]]
    assert received == request


def test_zero_byte_send_reports_channel_failure(observed_session) -> None:
    class ClosedChannel(socket.socket):
        @override
        def send(self, data: Any, flags: int = 0) -> int:
            return 0

    local, peer = socket.socketpair()
    channel = ClosedChannel(fileno=local.detach())
    channel.setblocking(False)
    with channel, peer, tempfile.TemporaryFile("w+b") as stdin, io.BytesIO() as stdout:
        stdin.write(b"input")
        stdin.seek(0)
        with pytest.raises(rtt_module.RttClientError):
            rtt_module._RttRelay(
                channel, stdin.fileno(), stdout, observed_session(lambda: None)
            ).run()


def test_established_channel_closure_fails_while_session_is_running(observed_session) -> None:
    local, peer = socket.socketpair()
    local.setblocking(False)
    peer.close()
    with (
        local,
        tempfile.TemporaryFile("w+b") as stdin,
        io.BytesIO() as stdout,
        pytest.raises(rtt_module.RttClientError),
    ):
        rtt_module._RttRelay(local, stdin.fileno(), stdout, observed_session(lambda: None)).run()


@pytest.mark.parametrize("phase", ("terminal-setup", "relay"))
@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_terminal_and_socket_cleanup_preserve_primary_failure(
    monkeypatch, channels, observed_session, phase: str, failure_type: type[BaseException]
) -> None:
    failure = failure_type("primary failure")
    restore_failure = OSError("terminal cleanup failure")
    close_failure = OSError("socket cleanup failure")
    original = [1, 2, 3, rtt_module.termios.ICANON | rtt_module.termios.ECHO, 5, 6, [7]]
    restored = []

    class Channel(TrackedChannel):
        @override
        def close(self) -> None:
            super().close()
            raise close_failure

    def set_attributes(_fd: int, _when: int, attributes: list) -> None:
        if attributes == original:
            restored.append(attributes)
            raise restore_failure
        if phase == "terminal-setup":
            raise failure

    def poll_session() -> int | None:
        raise failure

    channels(Channel)
    monkeypatch.setattr(rtt_module.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(rtt_module.termios, "tcgetattr", lambda _fd: list(original))
    monkeypatch.setattr(rtt_module.termios, "tcsetattr", set_attributes)
    server = RttServer(idle)
    with (
        tempfile.TemporaryFile("w+b") as stdin,
        io.BytesIO() as stdout,
        pytest.raises(failure_type) as raised,
    ):
        rtt_module.run_rtt_client(
            server.port, observed_session(poll_session), stdin=stdin, stdout=stdout
        )

    assert raised.value is failure
    assert restored == [original]
    notes = "\n".join(raised.value.__notes__)
    assert str(restore_failure) in notes
    assert str(close_failure) in notes
    server.join()


@pytest.mark.parametrize("cleanup", ("terminal", "socket"))
def test_cleanup_failure_is_reported_after_session_exit(
    monkeypatch, channels, observed_session, cleanup: str
) -> None:
    failure = OSError("cleanup failure")
    original = [1, 2, 3, rtt_module.termios.ICANON | rtt_module.termios.ECHO, 5, 6, [7]]

    class Channel(TrackedChannel):
        @override
        def close(self) -> None:
            super().close()
            if cleanup == "socket":
                raise failure

    def set_attributes(_fd: int, _when: int, attributes: list) -> None:
        if attributes == original and cleanup == "terminal":
            raise failure

    channels(Channel)
    monkeypatch.setattr(rtt_module.os, "isatty", lambda _fd: True)
    monkeypatch.setattr(rtt_module.termios, "tcgetattr", lambda _fd: list(original))
    monkeypatch.setattr(rtt_module.termios, "tcsetattr", set_attributes)
    server = RttServer(idle)
    with (
        tempfile.TemporaryFile("w+b") as stdin,
        io.BytesIO() as stdout,
        pytest.raises(OSError) as raised,
    ):
        rtt_module.run_rtt_client(
            server.port, observed_session(lambda: 0), stdin=stdin, stdout=stdout
        )

    assert raised.value is failure
    server.join()
