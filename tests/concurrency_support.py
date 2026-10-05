# SPDX-License-Identifier: Apache-2.0

"""Deterministic external-process gates shared by session and runner tests."""

from __future__ import annotations

import json
import os
import select
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

from tests.process_support import read_line
from tests.support import ROOT


class ConcurrentSessions:
    """Check operation progress while another acquired session remains active."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "home").mkdir()
        (root / "runtime").mkdir()
        self.image = root / "firmware.hex"
        self.image.write_text(":00000001FF\n", encoding="utf-8")

    @property
    def ssh_argv(self) -> tuple[str, ...]:
        return (sys.executable, str(ROOT / "tests/fixtures/local_ssh.py"), str(self.root))

    @property
    def child_argv(self) -> tuple[str, ...]:
        return (
            sys.executable,
            str(ROOT / "tests/fixtures/session_child.py"),
            str(self.root),
        )

    def check_progress(
        self,
        operation: Callable[[int], None],
        *,
        opened: tuple[threading.Event, threading.Event],
    ) -> None:
        """The second operation must finish while the first session stays open."""
        workspaces: list[Path] = []
        child_pidfds: list[int] = []
        with ExitStack() as resources:
            entered_streams = []
            gates = []
            for channel in (0, 1):
                entered = self.root / f"entered-{channel}"
                gate = self.root / f"gate-{channel}"
                os.mkfifo(entered)
                os.mkfifo(gate)
                entered_streams.append(
                    resources.enter_context(
                        os.fdopen(os.open(entered, os.O_RDWR | os.O_NONBLOCK), "rb", buffering=0)
                    )
                )
                # O_RDWR lets teardown release a gate even if startup never spawned its child.
                gate_fd = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
                resources.callback(os.close, gate_fd)
                gates.append(gate_fd)

            def observe_entry(channel: int) -> None:
                entry = json.loads(read_line(entered_streams[channel], timeout=10))
                workspace = Path(entry["workspace"])
                assert workspace.is_dir()
                workspaces.append(workspace)
                child_pidfd = os.pidfd_open(entry["pid"])
                resources.callback(os.close, child_pidfd)
                child_pidfds.append(child_pidfd)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(operation, 0)
                    second = None
                    try:
                        observe_entry(0)
                        assert opened[0].wait(10), "first session did not finish opening"
                        second = executor.submit(operation, 1)
                        observe_entry(1)
                        assert opened[1].wait(10), "second session did not finish opening"
                        os.write(gates[1], b"x")
                        second.result(timeout=30)
                        assert not first.done(), "first session must remain active until released"
                    finally:
                        for gate_fd in gates:
                            os.write(gate_fd, b"x")
                        first.result(timeout=30)
                        if second is not None:
                            second.result(timeout=30)
            finally:
                # Executor shutdown joins both workers before cleanup is inspected,
                # including when an operation raises during startup, waiting, or close.
                assert all(not workspace.exists() for workspace in workspaces)
                for pidfd in child_pidfds:
                    poller = select.poll()
                    poller.register(pidfd, select.POLLIN)
                    assert poller.poll(30_000), "child survived session cleanup"

        assert len(workspaces) == 2
        assert workspaces[0] != workspaces[1]
