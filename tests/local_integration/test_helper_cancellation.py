# SPDX-License-Identifier: Apache-2.0

"""Ctrl-C during helper startup, START delivery, and READY observation."""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path, PurePosixPath
from typing import override

import pytest
from zephyr_remote_openocd.controller import helper_connection as connection_module
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller.api import (
    SessionController,
    SessionPlan,
    SshCommand,
    StagedFile,
)
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import encode_frame

from tests.control_support import ControlProcess
from tests.process_support import cleanup_on_exit
from tests.session_support import NotifiedAuthority
from tests.support import ROOT

pytestmark = [pytest.mark.local, pytest.mark.skipif(os.name != "posix", reason="POSIX signals")]

GUARD = 60
CREATED = encode_frame(
    "SESSION_CREATED", helper="test", session_id="session", remote_workspace="/workspace"
)


def helper_code(entered: Path, *, created: bool, read_start: bool) -> str:
    """A control helper that reports entry through a FIFO, then stays silent.

    Without reading START it leaves controller input unread until EOF, so a
    large START applies pipe backpressure to the coordinator.
    """
    return (
        "import os, select, signal, sys\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        + (f"os.write(1, {CREATED!r})\n" if created else "")
        + ("sys.stdin.buffer.readline()\n" if read_start else "")
        + f"open({str(entered)!r}, 'wb').close()\n"
        "poller = select.poll()\n"
        "poller.register(0, select.POLLHUP)\n"
        "while not poller.poll():\n"
        "    pass\n"
    )


def interrupt_after(entered: Path) -> threading.Thread:
    def interrupt() -> None:
        # Native delivery reaches only this thread; the coordinator is never
        # interrupted in a system call and must be woken by its own wait.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
        with entered.open("rb") as stream:
            stream.read()
        os.kill(os.getpid(), signal.SIGINT)

    sender = threading.Thread(target=interrupt)
    sender.start()
    return sender


def run_cancelled(
    tmp_path: Path, notified_authority: NotifiedAuthority, *, created: bool, read_start: bool
) -> tuple[HelperConnection, BaseException | None]:
    entered = tmp_path / "entered"
    os.mkfifo(entered)
    helper = HelperConnection(
        SshCommand(
            (sys.executable, "-c", helper_code(entered, created=created, read_start=read_start))
        ),
        "host",
        DeploymentResult("/helper.py", "digest", False),
        authority=notified_authority.authority,
        notifier=notified_authority.notifier,
    )
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    sender = interrupt_after(entered)
    # START exceeds the pipe capacity so an unread helper applies backpressure.
    process = ProcessSpec(("child", "x" * 512 * 1024))
    try:
        with pytest.raises(OperationCancelled), operation_scope():
            try:
                helper.acquire()
                helper.start_process(process, ())
                pytest.fail("cancellation did not interrupt the helper wait")
            finally:
                helper.close()
    finally:
        sender.join(GUARD)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    return helper, notified_authority.authority.snapshot().failure


@pytest.mark.timeout(GUARD)
@pytest.mark.parametrize(
    ("created", "read_start"),
    ((False, False), (True, False), (True, True)),
    ids=("session-created", "start-backpressure", "ready"),
)
def test_ctrl_c_interrupts_helper_waits_and_settles_control(
    tmp_path, notified_authority, created, read_start
) -> None:
    helper, failure = run_cancelled(
        tmp_path, notified_authority, created=created, read_start=read_start
    )
    # Cancellation is the recorded primary outcome; the transport is reaped.
    assert isinstance(failure, OperationCancelled)
    (process,) = (helper._process,)
    assert process is not None and process.returncode is not None
    assert process.stdin is not None and process.stdin.closed
    assert helper._join_reader(0)


@pytest.mark.timeout(GUARD)
def test_ctrl_c_during_local_archive_work_is_acted_on_at_the_next_boundary(
    tmp_path, monkeypatch
) -> None:
    for directory in ("home", "runtime"):
        (tmp_path / directory).mkdir()
    image = tmp_path / "firmware.hex"
    image.write_text(":00000001FF\n", encoding="utf-8")
    # The configured SSH command records every remote command it executes.
    executed = tmp_path / "executed"
    recording_ssh = tmp_path / "recording-ssh"
    recording_ssh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"open({str(executed)!r}, 'a').write(sys.argv[-1] + '\\n')\n"
        f"os.execv(sys.executable, [sys.executable, {str(ROOT / 'tests/fixtures/local_ssh.py')!r},"
        f" {str(tmp_path)!r}, *sys.argv[1:]])\n"
    )
    recording_ssh.chmod(0o755)
    plan = SessionPlan(
        "test-host",
        SshCommand((str(recording_ssh),)),
        ProcessSpec(
            (sys.executable, "-c", "pass"), completion_policy=CompletionPolicy.PROCESS_EXIT
        ),
        staged_files=(StagedFile(image, PurePosixPath("staged/firmware.hex")),),
    )
    real_build = session_module.build_archive

    def build_during_ctrl_c(files):
        # Local CPU/filesystem work is not interrupted; the fact is retained.
        archive = real_build(files)
        signal.raise_signal(signal.SIGINT)
        return archive

    monkeypatch.setattr(session_module, "build_archive", build_during_ctrl_c)
    with pytest.raises(OperationCancelled), operation_scope():
        session = SessionController.prepare(plan)
        with cleanup_on_exit(session.close):
            session.acquire()
    # Deployment and control ran; staging was refused at its admission.
    commands = executed.read_text().splitlines()
    assert any(command.endswith(" control") for command in commands)
    assert not any(" stage " in command for command in commands)
    assert session.closed and session.descriptor is None
    sessions = tmp_path / "runtime" / "zephyr_remote_openocd-sessions-v1"
    # The allocated remote workspace and its metadata were cleaned up.
    assert sessions.is_dir() and not tuple(sessions.iterdir())


@pytest.mark.parametrize("primary", ("cancellation", "command-failure", "none"))
def test_staging_archive_close_failure_preserves_operation_outcome(tmp_path, monkeypatch, primary):
    for directory in ("home", "runtime"):
        (tmp_path / directory).mkdir()
    command_failure = OSError("staging command failed")
    cleanup = OSError("staging archive close failed")
    cleanup.add_note("retained archive cleanup detail")
    real_build = session_module.build_archive
    archives = []

    def build_with_close_failure(files):
        archive = real_build(files)
        archives.append(archive)
        close = archive.stream.close

        def fail_close():
            close()
            raise cleanup

        monkeypatch.setattr(archive.stream, "close", fail_close)
        return archive

    class Command(SshCommand):
        @override
        def run_stream(self, host, remote_command, stream, *, timeout=60):
            if primary == "cancellation":
                signal.raise_signal(signal.SIGINT)
            elif primary == "command-failure":
                raise command_failure
            return super().run_stream(host, remote_command, stream, timeout=timeout)

    monkeypatch.setattr(session_module, "build_archive", build_with_close_failure)
    plan = SessionPlan(
        "test-host",
        Command((sys.executable, str(ROOT / "tests/fixtures/local_ssh.py"), str(tmp_path))),
        ProcessSpec(("unused",)),
    )
    with pytest.raises(BaseException) as raised, operation_scope() as operation:
        session = SessionController.prepare(plan)
        try:
            session.acquire()
        except BaseException as error:
            session.record_operation_failure(error)
        finally:
            session.finish()

    expected = (
        operation.cancellation
        if primary == "cancellation"
        else command_failure
        if primary == "command-failure"
        else cleanup
    )
    assert raised.value is expected
    assert session._authority.snapshot().failure is expected
    assert session.closed and session.descriptor is None
    assert archives and all(archive.stream.closed for archive in archives)
    if primary != "none":
        notes = getattr(raised.value, "__notes__", ())
        assert any(str(cleanup) in note for note in notes)
        assert any("retained archive cleanup detail" in note for note in notes)
    sessions = tmp_path / "runtime" / "zephyr_remote_openocd-sessions-v1"
    assert sessions.is_dir() and not tuple(sessions.iterdir())


def test_latched_cancellation_wins_over_an_expired_helper_startup(monkeypatch) -> None:
    process = ControlProcess(b'{"version":1,"type":"SESSION_CREATED"')

    class Command(SshCommand):
        @override
        def popen(self, host, remote_command, *, local_forward=None, on_acquired=None):
            del host, remote_command, local_forward
            if on_acquired is not None:
                on_acquired(process)
            return process

    wiring = NotifiedAuthority()
    helper = HelperConnection(
        Command(("unused-ssh",)),
        "host",
        DeploymentResult("/helper.py", "digest", False),
        authority=wiring.authority,
        notifier=wiring.notifier,
    )
    # The startup deadline has already expired when the coordinator first waits.
    monkeypatch.setattr(connection_module, "HELPER_START_TIMEOUT", 0.0)
    try:
        with pytest.raises(OperationCancelled), operation_scope():
            signal.raise_signal(signal.SIGINT)
            try:
                helper.acquire()
            finally:
                helper.close()
    finally:
        process.end_events()
        wiring.close()
    assert process.returncode is not None
