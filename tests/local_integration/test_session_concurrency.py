# SPDX-License-Identifier: Apache-2.0

"""Independent sessions must progress through the real helper without hardware."""

from __future__ import annotations

import os
import select
import signal
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import FrameType
from typing import override

import pytest
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.remote import RemoteSession
from zephyr_remote_openocd.remote.flash import FlashInputs, build_flash_plan
from zephyr_remote_openocd.remote.model import RemoteSessionRequest
from zephyr_remote_openocd.remote.paths import PathPlanner
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, SshCommand, SshLocalForward

from tests.concurrency_support import ConcurrentSessions
from tests.process_support import cleanup_on_exit
from tests.support import ROOT

pytestmark = pytest.mark.local


@pytest.mark.parametrize("phase", ("acquire-return", "caller-resumption"))
def test_interruption_after_session_acquisition_cleans_helper_and_child(tmp_path: Path, phase: str):
    (tmp_path / "home").mkdir()
    (tmp_path / "runtime").mkdir()
    child_pid_path = tmp_path / "child.pid"
    transports: list[ManagedSshProcess] = []

    class Command(SshCommand):
        @override
        def popen(
            self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
        ) -> ManagedSshProcess:
            process = super().popen(host, remote_command, local_forward=local_forward)
            transports.append(process)
            return process

    child = ProcessSpec(
        (
            sys.executable,
            "-c",
            "import os,pathlib,signal,sys; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "print('ZRO_TEST_READY', flush=True); signal.pause()",
            str(child_pid_path),
        ),
        required_output_sentinels=("ZRO_TEST_READY",),
    )
    session = RemoteSession.prepare(
        RemoteSessionRequest(
            "test-host",
            Command((sys.executable, str(ROOT / "tests/fixtures/local_ssh.py"), str(tmp_path))),
            child,
        )
    )
    interruption = KeyboardInterrupt("session acquisition interrupted")
    previous_handler = signal.getsignal(signal.SIGINT)
    previous_trace = sys.gettrace()
    child_pidfd: int | None = None
    workspace: Path | None = None
    interrupted = False

    def handle_interrupt(_signum, _frame):
        raise interruption

    def acquire_owned_session():
        with cleanup_on_exit(session.close):
            session.acquire()
            pytest.fail("interruption boundary was not reached")

    def interrupt_at_boundary(frame: FrameType, event: str, _arg: object):
        nonlocal child_pidfd, workspace, interrupted
        acquire_return = event == "return" and frame.f_code is RemoteSession.acquire.__code__
        caller_resumption = (
            event == "line"
            and frame.f_code is acquire_owned_session.__code__
            and session.descriptor is not None
        )
        if not interrupted and (
            (phase == "acquire-return" and acquire_return)
            or (phase == "caller-resumption" and caller_resumption)
        ):
            assert session.descriptor is not None
            workspace = Path(session.descriptor.remote_workspace)
            child_pidfd = os.pidfd_open(int(child_pid_path.read_text()))
            interrupted = True
            signal.raise_signal(signal.SIGINT)
        return interrupt_at_boundary

    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            sys.settrace(interrupt_at_boundary)
            acquire_owned_session()
        sys.settrace(previous_trace)
        assert raised.value is interruption
        assert interrupted and session.closed
        assert workspace is not None
        assert not any(
            path.exists()
            for path in (
                workspace,
                workspace.with_name(f".{workspace.name}.lease"),
                workspace.with_name(f".{workspace.name}.closed"),
            )
        )
        assert child_pidfd is not None
        poller = select.poll()
        poller.register(child_pidfd, select.POLLIN)
        assert poller.poll(30_000), "child survived session cleanup"
        assert transports and all(process.poll() is not None for process in transports)
        for process in transports:
            assert process.stdin is not None and process.stdin.closed
            assert process.stdout is not None and process.stdout.closed
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous_handler)
        session.close()
        if child_pidfd is not None:
            os.close(child_pidfd)


def test_same_probe_session_finishes_while_another_session_is_active(tmp_path: Path) -> None:
    concurrent = ConcurrentSessions(tmp_path)
    ssh = SshCommand(concurrent.ssh_argv)
    opened = (Event(), Event())

    def operation(channel: int) -> None:
        plan = build_flash_plan(
            FlashInputs(
                executable=concurrent.child_argv,
                image_type="hex",
                file=str(concurrent.image),
                elf_file=None,
                hex_file=None,
                bin_file=None,
                search_paths=(),
                config_files=(),
                load_command="program",
                verify_command="verify_image",
                serial="test-probe",
                pre_init=(f"test_channel {channel}",),
            ),
            PathPlanner(()),
        )
        session = RemoteSession.prepare(
            RemoteSessionRequest(
                "test-host",
                ssh,
                replace(plan.process, required_output_sentinels=("ZRO_TEST_READY",)),
                staged_files=plan.staged_files,
            )
        )
        try:
            session.acquire()
            assert session.check_openocd_exit() is None
            opened[channel].set()
            assert session.wait_for_openocd_exit(30) == 0
        finally:
            session.close()

    concurrent.check_progress(operation, opened=opened)
