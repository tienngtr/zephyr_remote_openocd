# SPDX-License-Identifier: Apache-2.0

"""Ctrl-C while forwarding readiness, session observation, and RTT relay wait."""

from __future__ import annotations

import fcntl
import io
import mmap
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Buffer, Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override

import pytest
from zephyr_remote_openocd.controller import _rtt as rtt_module
from zephyr_remote_openocd.controller._forwarding import ForwardManager
from zephyr_remote_openocd.controller._rtt import run_rtt_client
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    ProcessAcquiredHandler,
    SshCommand,
    SshForwardSpec,
)
from zephyr_remote_openocd.controller.authority import SessionAuthority
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec

from tests.forwarding_support import GDB, ControlledSshCommand, ForwardingHarness
from tests.process_support import cleanup_on_exit
from tests.session_support import ObservedStatusFactory

pytestmark = [pytest.mark.local, pytest.mark.skipif(os.name != "posix", reason="POSIX signals")]

GUARD = 60


@contextmanager
def interrupt_when(
    ready: Callable[[], object], release: Callable[[], None] = lambda: None
) -> Iterator[None]:
    """Deliver native SIGINT to a helper thread once ``ready`` returns.

    The coordinator blocks SIGINT, so it is never interrupted inside a system
    call; only its event-driven wait can observe the cancellation.
    """
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})

    def interrupt() -> None:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
        ready()
        os.kill(os.getpid(), signal.SIGINT)

    sender = threading.Thread(target=interrupt)
    sender.start()
    try:
        yield
    finally:
        # A failed scenario must not strand the sender at its readiness gate.
        release()
        sender.join(GUARD)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def read_fifo(path: Path) -> Callable[[], None]:
    def wait_for_writer() -> None:
        with path.open("rb") as stream:
            stream.read()

    return wait_for_writer


def release_fifo(path: Path) -> Callable[[], None]:
    def open_writer() -> None:
        with suppress(OSError):
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))

    return open_writer


@pytest.mark.timeout(GUARD)
def test_ctrl_c_during_forward_readiness_rolls_back_the_transport(tmp_path: Path) -> None:
    entered = tmp_path / "entered"
    os.mkfifo(entered)
    pids = tmp_path / "pids"
    # A forward transport that ignores SSH arguments, starts, and never
    # reports readiness.
    launcher = tmp_path / "configured-ssh"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        f"open({str(pids)!r}, 'w').write(str(os.getpid()))\n"
        f"open({str(entered)!r}, 'wb').close()\n"
        "sys.stdin.buffer.read()\n"
    )
    launcher.chmod(0o755)
    manager = ForwardManager(SshCommand((str(launcher),)), "host")
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with (
        interrupt_when(read_fifo(entered), release_fifo(entered)),
        pytest.raises(OperationCancelled),
        operation_scope(),
    ):
        manager.start((ForwardSpec("gdb", port, 3333),), "127.64.0.1")
        pytest.fail("cancellation did not interrupt forward readiness")
    assert not manager.has_forwards
    pid = int(pids.read_text())
    with pytest.raises(ChildProcessError):
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG)


@pytest.mark.timeout(GUARD)
def test_ctrl_c_during_rtt_relay_closes_the_channel(
    observed_session: ObservedStatusFactory,
) -> None:
    accepted = threading.Event()
    closed = threading.Event()

    def serve(listener: socket.socket) -> None:
        with listener, listener.accept()[0] as connection:
            accepted.set()
            while connection.recv(4096):
                pass
            closed.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = threading.Thread(target=serve, args=(listener,))
    server.start()
    try:
        with (
            tempfile.TemporaryFile("w+b") as stdin,
            io.BytesIO() as stdout,
            interrupt_when(lambda: accepted.wait(GUARD)),
            pytest.raises(OperationCancelled),
            operation_scope(),
        ):
            run_rtt_client(
                listener.getsockname()[1],
                observed_session(lambda: None),
                stdin=stdin,
                stdout=stdout,
            )
            pytest.fail("cancellation did not interrupt the RTT relay")
    finally:
        server.join(GUARD)
    # The client released its forwarded channel during cancellation.
    assert closed.is_set()


@pytest.mark.parametrize("work", ("forward", "local-client"))
def test_ctrl_c_latched_during_session_admission_prevents_the_launch(
    monkeypatch: pytest.MonkeyPatch, work: str
) -> None:
    harness = ForwardingHarness(monkeypatch)
    launched: list[str] = []
    admit_work = SessionAuthority.admit_work
    popen = ControlledSshCommand.popen

    def record_popen(
        self: ControlledSshCommand,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired: ProcessAcquiredHandler | None = None,
    ) -> ManagedSshProcess:
        launched.append("forward")
        return popen(
            self, host, remote_command, local_forward=local_forward, on_acquired=on_acquired
        )

    def admit_after_ctrl_c(
        self: SessionAuthority,
        required: Iterable[ForwardSpec],
        forwarded: Iterable[ForwardSpec],
        **kwargs: Callable[[], None],
    ) -> None:
        # Ctrl-C latches after any earlier cancellation observation, while the
        # session's own admission conditions are being evaluated.
        signal.raise_signal(signal.SIGINT)
        admit_work(self, required, forwarded, **kwargs)

    monkeypatch.setattr(ControlledSshCommand, "popen", record_popen)
    harness.ssh.process(GDB)
    with pytest.raises(OperationCancelled), operation_scope():
        session = harness.prepare((GDB,), ()) if work == "local-client" else harness.prepare((), ())
        with cleanup_on_exit(session.close):
            session.acquire()
            monkeypatch.setattr(SessionAuthority, "admit_work", admit_after_ctrl_c)
            if work == "forward":
                session.forward((GDB,))
            else:
                session.run_local_client(lambda: launched.append("client"))
    # The latched cancellation preceded final admission: nothing new started.
    assert launched == ([] if work == "forward" else ["forward"])
    assert harness.helper.close_calls == 1


@pytest.mark.timeout(GUARD)
def test_ctrl_c_after_launch_admission_leaves_the_transport_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "configured-ssh"
    launcher.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.buffer.read()\n")
    launcher.chmod(0o755)
    initialize = subprocess.Popen.__init__
    pids: list[int] = []

    # Popen's arguments pass through unchanged.
    def launch_after_admission(process: subprocess.Popen[bytes], *args: Any, **kwargs: Any) -> None:
        # Ctrl-C latches after the launch was admitted, during acquisition.
        signal.raise_signal(signal.SIGINT)
        initialize(process, *args, **kwargs)
        pids.append(process.pid)

    manager = ForwardManager(SshCommand((str(launcher),)), "host")
    monkeypatch.setattr(subprocess.Popen, "__init__", launch_after_admission)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    with pytest.raises(OperationCancelled), operation_scope():
        manager.start((ForwardSpec("gdb", port, 3333),), "127.64.0.1")
    monkeypatch.undo()
    # The admitted acquisition completed into rollback ownership and was settled.
    assert len(pids) == 1
    pid = pids[0]
    with pytest.raises(ChildProcessError):
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG)
    assert not manager.has_forwards


def test_latched_cancellation_wins_over_an_expired_session_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = ForwardingHarness(monkeypatch)
    harness.helper.on_wait = lambda: None
    with pytest.raises(OperationCancelled), operation_scope():
        session = harness.prepare((GDB,), ())
        with cleanup_on_exit(session.close):
            session.acquire()
            signal.raise_signal(signal.SIGINT)
            # The deadline has already expired when the wait arbitrates.
            session.wait_for_openocd_exit(0)


def test_cancellation_during_failed_rtt_attempt_wins_over_expired_startup(
    observed_session: ObservedStatusFactory,
) -> None:
    class ClosingChannel(socket.socket):
        @override
        def close(self) -> None:
            # Ctrl-C latches while the refused attempt's socket is released.
            signal.raise_signal(signal.SIGINT)
            super().close()

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    client_socket = SimpleNamespace(**vars(socket))
    client_socket.socket = ClosingChannel
    with (
        pytest.MonkeyPatch.context() as patch,
        tempfile.TemporaryFile("w+b") as stdin,
        io.BytesIO() as stdout,
        pytest.raises(OperationCancelled),
        operation_scope(),
    ):
        patch.setattr(rtt_module, "socket", client_socket)
        run_rtt_client(
            port, observed_session(lambda: None), stdin=stdin, stdout=stdout, startup_timeout=0
        )


@pytest.mark.timeout(GUARD)
def test_ctrl_c_while_the_rtt_output_consumer_stalls_closes_the_channel(
    observed_session: ObservedStatusFactory,
) -> None:
    read_end, write_end = os.pipe2(os.O_DIRECT)
    # Packet mode gives each relayed write its own pipe buffer, so the pipe is
    # full exactly when it stops reporting writable.
    fcntl.fcntl(write_end, fcntl.F_SETPIPE_SZ, mmap.PAGESIZE)
    buffers = fcntl.fcntl(write_end, fcntl.F_GETPIPE_SZ) // mmap.PAGESIZE
    payload = b"x" * ((buffers + 2) * 4096)
    undeliverable = threading.Event()
    closed = threading.Event()

    def consumer_writable() -> bool:
        poller = select.poll()
        poller.register(write_end, select.POLLOUT)
        return bool(poller.poll(0))

    class ObservedChannel(socket.socket):
        @override
        def recv(self, size: int, flags: int = 0, /) -> bytes:
            data = super().recv(size, flags)
            if data and not consumer_writable():
                # The client now holds output the stalled consumer cannot take.
                undeliverable.set()
            return data

    def serve(listener: socket.socket) -> None:
        with listener, listener.accept()[0] as connection:
            with suppress(OSError):
                connection.sendall(payload)
                while connection.recv(4096):
                    pass
            closed.set()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = threading.Thread(target=serve, args=(listener,))
    server.start()
    client_socket = SimpleNamespace(**vars(socket))
    client_socket.socket = ObservedChannel
    try:
        with (
            pytest.MonkeyPatch.context() as patch,
            os.fdopen(write_end, "wb") as stdout,
            # The consumer never reads; it goes away before the output closes.
            os.fdopen(read_end, "rb"),
            tempfile.TemporaryFile("w+b") as stdin,
            interrupt_when(lambda: undeliverable.wait(GUARD)),
            pytest.raises(OperationCancelled),
            operation_scope(),
        ):
            patch.setattr(rtt_module, "socket", client_socket)
            run_rtt_client(
                listener.getsockname()[1],
                observed_session(lambda: None),
                stdin=stdin,
                stdout=stdout,
            )
            pytest.fail("cancellation did not interrupt the RTT relay")
    finally:
        server.join(GUARD)
    # The client released its forwarded channel during cancellation.
    assert closed.is_set()


@pytest.mark.timeout(GUARD)
def test_ctrl_c_while_helper_output_holds_the_rtt_stream_closes_the_channel(
    observed_session: ObservedStatusFactory, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class StalledOutput(io.FileIO):
        @override
        def write(self, data: Buffer, /) -> int:
            # Helper output delivery is blocked mid-write, holding the shared
            # buffered stream that RTT output also uses.
            entered.set()
            release.wait(GUARD)
            return super().write(data)

    class CtrlCAtInputAdoption(io.FileIO):
        @override
        def fileno(self) -> int:
            # Ctrl-C latches after connection setup, as the client adopts stdin.
            signal.raise_signal(signal.SIGINT)
            return super().fileno()

    def serve(listener: socket.socket) -> None:
        with listener, listener.accept()[0] as connection:
            while connection.recv(4096):
                pass
            closed.set()

    def deliver_helper_output(stream: io.BufferedWriter) -> None:
        stream.write(b"helper output\n")
        stream.flush()

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = threading.Thread(target=serve, args=(listener,))
    server.start()
    read_end, write_end = os.pipe()
    try:
        with (
            os.fdopen(read_end, "rb") as consumer,
            io.BufferedWriter(StalledOutput(write_end, "wb")) as stdout,
            CtrlCAtInputAdoption(tmp_path / "stdin", "w+b") as stdin,
        ):
            helper_output = threading.Thread(target=deliver_helper_output, args=(stdout,))
            helper_output.start()
            try:
                assert entered.wait(GUARD)
                with pytest.raises(OperationCancelled), operation_scope():
                    run_rtt_client(
                        listener.getsockname()[1],
                        observed_session(lambda: None),
                        stdin=stdin,
                        stdout=stdout,
                    )
                    pytest.fail("cancellation did not interrupt the RTT relay")
                # The relay released its channel while helper output still held
                # the stream.
                assert closed.wait(GUARD)
                assert helper_output.is_alive()
            finally:
                release.set()
                helper_output.join(GUARD)
            assert consumer.read1(4096) == b"helper output\n"
    finally:
        server.join(GUARD)
