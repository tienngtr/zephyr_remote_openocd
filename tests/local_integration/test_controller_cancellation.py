# SPDX-License-Identifier: Apache-2.0

"""GC callback interruption through real controller and helper lifecycles."""

from __future__ import annotations

import os
import select
import selectors
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller import rtt as rtt_module
from zephyr_remote_openocd.controller.cancellation import local_cancellation
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.plan import SessionPlan
from zephyr_remote_openocd.controller.session import SessionController
from zephyr_remote_openocd.controller.ssh import ManagedSshProcess, SshCommand
from zephyr_remote_openocd.protocol.process import ProcessSpec

from tests.cancellation_support import deliver_sigint_in_gc
from tests.process_support import cleanup_on_exit
from tests.support import ROOT

pytestmark = [pytest.mark.local, pytest.mark.usefixtures("enabled_cyclic_gc")]


@pytest.mark.parametrize("boundary", ("helper-adoption", "reader-adoption", "active-wait", "close"))
def test_gc_signal_cancels_owned_session_and_reaps_child(tmp_path: Path, monkeypatch, boundary):
    (tmp_path / "home").mkdir()
    (tmp_path / "runtime").mkdir()
    transports: list[ManagedSshProcess] = []
    original_popen = SshCommand.popen

    def popen(command, host, remote_command, *, local_forward=None):
        process = original_popen(command, host, remote_command, local_forward=local_forward)
        transports.append(process)
        return process

    monkeypatch.setattr(SshCommand, "popen", popen)
    child = ProcessSpec(
        (
            sys.executable,
            "-c",
            "import signal; print('ZRO_TEST_READY', flush=True); signal.pause()",
        ),
        required_output_sentinels=("ZRO_TEST_READY",),
    )
    session = SessionController.prepare(
        SessionPlan(
            "test-host",
            SshCommand((sys.executable, str(ROOT / "tests/fixtures/local_ssh.py"), str(tmp_path))),
            child,
        )
    )
    native_mask = signal.pthread_sigmask
    delivered = False
    workspace: Path | None = None
    child_pidfd: int | None = None
    original_dispatch = HelperConnection._dispatch

    def dispatch(connection, event):
        nonlocal child_pidfd, workspace
        if event["type"] == "READY":
            child_pidfd = os.pidfd_open(event["child_pid"])
            workspace = Path(connection.allocation.remote_workspace)
        original_dispatch(connection, event)

    monkeypatch.setattr(HelperConnection, "_dispatch", dispatch)

    def interrupt():
        nonlocal delivered
        delivered = True
        deliver_sigint_in_gc()

    boundaries = {
        "helper-adoption": HelperConnection._acquire.__code__,
        "reader-adoption": HelperConnection._start_event_drain.__code__,
        "close": SessionController.close.__code__,
    }

    def restore_and_interrupt(how, mask):
        previous = native_mask(how, mask)
        if (
            not delivered
            and how == signal.SIG_SETMASK
            and signal.SIGINT not in mask
            and sys._getframe(1).f_code is boundaries.get(boundary)
        ):
            interrupt()
        return previous

    monkeypatch.setattr(signal, "pthread_sigmask", restore_and_interrupt)
    if boundary == "active-wait":
        original_wait = HelperConnection.wait_for_change

        def wait_and_interrupt(connection, timeout):
            interrupt()
            original_wait(connection, timeout)

        monkeypatch.setattr(HelperConnection, "wait_for_change", wait_and_interrupt)
    try:
        with pytest.raises(KeyboardInterrupt), local_cancellation(), cleanup_on_exit(session.close):
            try:
                session.acquire()
                if boundary == "active-wait":
                    session.wait_for_openocd_exit()
                elif boundary == "close":
                    session.close()
                else:
                    pytest.fail("startup published after GC callback interruption")
            except BaseException as error:
                session.record_operation_failure(error)
                raise
        assert delivered and session.closed
        assert transports and all(process.poll() is not None for process in transports)
        assert all(process.stdin.closed and process.stdout.closed for process in transports)
        assert not list((tmp_path / "runtime/zephyr_remote_openocd").glob("*/staged"))
        if workspace is not None:
            assert not workspace.exists()
        if child_pidfd is not None:
            poller = select.poll()
            poller.register(child_pidfd, select.POLLIN)
            assert poller.poll(30_000), "OpenOCD child survived cancellation cleanup"
    finally:
        session.close()
        if child_pidfd is not None:
            os.close(child_pidfd)


@pytest.mark.parametrize("write", (False, True), ids=("readiness-read", "start-write"))
def test_gc_signal_wakes_a_stalled_startup_pipe(monkeypatch, write):
    class InterruptingSelector(selectors.EpollSelector):
        def select(self, timeout=None):
            deliver_sigint_in_gc()
            return super().select(timeout)

    monkeypatch.setattr(selectors, "DefaultSelector", InterruptingSelector)
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb", buffering=0) as reader,
        os.fdopen(write_fd, "wb", buffering=0) as writer,
    ):
        if write:
            os.set_blocking(write_fd, False)
            try:
                while True:
                    os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                pass
            os.set_blocking(write_fd, True)
        with pytest.raises(KeyboardInterrupt), local_cancellation():
            if write:
                HelperConnection._write_command(writer, b"START")
            else:
                HelperConnection._read_initial_message(reader, None)
        assert os.get_blocking(write_fd)


@pytest.mark.parametrize("stream_input", (False, True), ids=("deployment", "staging"))
def test_gc_signal_during_finite_ssh_io_stops_and_closes_the_owned_command(
    tmp_path, monkeypatch, stream_input
):
    processes = []
    original_popen = subprocess.Popen
    original_communicate = ManagedSshProcess.communicate
    delivered = False

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def communicate(owner, payload, timeout):
        nonlocal delivered
        if not delivered:
            delivered = True
            deliver_sigint_in_gc()
        return original_communicate(owner, payload, timeout)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(ManagedSshProcess, "communicate", communicate)
    command = SshCommand((sys.executable, "-c", "import signal; signal.pause()"))
    source_path = tmp_path / "payload"
    source_path.write_bytes(b"payload" * 10000)
    try:
        with (
            source_path.open("rb") as source,
            pytest.raises(KeyboardInterrupt),
            local_cancellation(),
        ):
            if stream_input:
                command.run_stream("test-host", "stage", source)
            else:
                command.run("test-host", "deploy", input_data=source.read())
        assert delivered and len(processes) == 1
        process = processes[0]
        assert process.poll() is not None
        assert process.stdout is not None and process.stderr is not None
        assert process.stdout.closed and process.stderr.closed
        if not stream_input:
            assert process.stdin is not None
            assert process.stdin.closed
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def test_gc_signal_after_rtt_socket_adoption_closes_the_owned_socket(monkeypatch):
    sockets = []
    native_socket = rtt_module.socket.socket
    native_mask = signal.pthread_sigmask
    delivered = False

    def create_socket():
        connection = native_socket()
        sockets.append(connection)
        return connection

    def restore_and_interrupt(how, mask):
        nonlocal delivered
        previous = native_mask(how, mask)
        if (
            not delivered
            and how == signal.SIG_SETMASK
            and signal.SIGINT not in mask
            and sys._getframe(1).f_code is rtt_module._RttConnection.acquire.__code__
        ):
            delivered = True
            deliver_sigint_in_gc()
        return previous

    monkeypatch.setattr(rtt_module.socket, "socket", create_socket)
    monkeypatch.setattr(signal, "pthread_sigmask", restore_and_interrupt)
    try:
        with pytest.raises(KeyboardInterrupt), local_cancellation():
            rtt_module.run_rtt_client(5566, lambda: None)
        assert delivered and len(sockets) == 1
        assert sockets[0].fileno() == -1
    finally:
        for connection in sockets:
            connection.close()
