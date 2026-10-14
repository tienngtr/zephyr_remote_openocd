# SPDX-License-Identifier: Apache-2.0

"""Ctrl-C while forwarding readiness, session observation, and RTT relay wait."""

from __future__ import annotations

import io
import os
import signal
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.forwarding import ForwardManager
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.rtt import run_rtt_client
from zephyr_remote_openocd.controller.ssh import SshCommand

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
def test_ctrl_c_during_forward_readiness_rolls_back_the_transport(tmp_path) -> None:
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
def test_ctrl_c_during_rtt_relay_closes_the_channel(observed_session) -> None:
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
