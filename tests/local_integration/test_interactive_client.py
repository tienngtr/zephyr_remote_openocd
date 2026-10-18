# SPDX-License-Identifier: Apache-2.0

"""Terminal Ctrl-C ownership across a real configured GDB wrapper and GDB.

Each scenario runs in its own session with a PTY as controlling terminal, so
terminal-generated SIGINT reaches only that foreground process group. The
runner side mirrors Zephyr's ``run_client()`` inside the interactive scope.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller import cancellation

from tests.process_support import read_line
from tests.support import ROOT

pytestmark = [
    pytest.mark.local,
    pytest.mark.skipif(os.name != "posix", reason="POSIX terminals"),
    pytest.mark.skipif(shutil.which("gdb") is None, reason="requires a local GDB"),
]

GUARD = 60

FIXTURE = ROOT / "tests/fixtures/interactive_client.py"


class Terminal:
    """The PTY master: user keystrokes in, GDB terminal output out."""

    def __init__(self, master: int) -> None:
        self.master = master
        self.output = bytearray()

    def expect(self, pattern: bytes) -> None:
        deadline = time.monotonic() + GUARD
        while pattern not in self.output:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"{pattern!r} not shown on terminal:\n{bytes(self.output)!r}"
            ready, _, _ = select.select((self.master,), (), (), remaining)
            if ready:
                try:
                    chunk = os.read(self.master, 4096)
                except OSError:
                    chunk = b""
                assert chunk, f"terminal closed before {pattern!r}:\n{bytes(self.output)!r}"
                self.output.extend(chunk)
        del self.output[: self.output.index(pattern) + len(pattern)]

    def type(self, keys: bytes) -> None:
        os.write(self.master, keys)


def run_scenario(mode: str, client: list[str]):
    environment = os.environ.copy()
    production = str(Path(cancellation.__file__).resolve().parents[2])
    repository = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (production, repository, environment.get("PYTHONPATH")))
    )
    master, slave = os.openpty()
    marker_read, marker_write = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            str(FIXTURE),
            "scenario",
            str(slave),
            str(marker_write),
            mode,
            sys.executable,
            str(FIXTURE),
            "wrapper",
            *client,
        ],
        start_new_session=True,
        pass_fds=(slave, marker_write),
        env=environment,
    )
    os.close(slave)
    os.close(marker_write)
    return process, Terminal(master), os.fdopen(marker_read, "rb", buffering=0)


def finish(process: subprocess.Popen, terminal: Terminal, markers, *, completed: bool) -> None:
    try:
        if completed:
            assert process.wait(GUARD) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(GUARD)
        markers.close()
        os.close(terminal.master)


GDB = [
    "gdb",
    "-nx",
    "-q",
    "-iex",
    "set debuginfod enabled off",
    "-iex",
    "set confirm off",
    "-iex",
    "set pagination off",
]


@pytest.mark.timeout(GUARD * 2)
def test_ctrl_c_at_the_gdb_prompt_belongs_to_gdb_until_restoration() -> None:
    process, terminal, markers = run_scenario("prompt", GDB)
    completed = False
    try:
        assert read_line(markers, timeout=GUARD) == b"capturing=True\n"
        assert read_line(markers, timeout=GUARD) == b"client-launched\n"
        # The configured wrapper starts with SIGINT ignored, as under stock Zephyr.
        terminal.expect(b"wrapper-sigint=ignored")
        terminal.expect(b"(gdb) ")
        terminal.type(b"\x03")
        terminal.expect(b"Quit")
        terminal.expect(b"(gdb) ")
        terminal.type(b"quit\n")
        # The client-interval Ctrl-C did not create runner cancellation.
        assert read_line(markers, timeout=GUARD) == b"restored requested=False\n"
        terminal.type(b"\x03")
        assert read_line(markers, timeout=GUARD) == b"cancelled\n"
        completed = True
    finally:
        finish(process, terminal, markers, completed=completed)


@pytest.mark.timeout(GUARD * 2)
def test_ctrl_c_interrupts_the_gdb_inferior_without_cancelling_the_runner() -> None:
    client = [*GDB, "-ex", "run", "--args", sys.executable, str(FIXTURE), "inferior"]
    process, terminal, markers = run_scenario("inferior", client)
    completed = False
    try:
        assert read_line(markers, timeout=GUARD) == b"capturing=True\n"
        assert read_line(markers, timeout=GUARD) == b"client-launched\n"
        terminal.expect(b"wrapper-sigint=ignored")
        terminal.expect(b"inferior-ready")
        terminal.type(b"\x03")
        terminal.expect(b"SIGINT")
        terminal.expect(b"(gdb) ")
        terminal.type(b"kill\nquit\n")
        assert read_line(markers, timeout=GUARD) == b"restored requested=False\n"
        terminal.type(b"\x03")
        assert read_line(markers, timeout=GUARD) == b"cancelled\n"
        completed = True
    finally:
        finish(process, terminal, markers, completed=completed)


@pytest.mark.timeout(GUARD * 2)
def test_cancellation_latched_before_entry_never_launches_gdb() -> None:
    process, terminal, markers = run_scenario("latched", GDB)
    completed = False
    try:
        assert read_line(markers, timeout=GUARD) == b"capturing=True\n"
        assert read_line(markers, timeout=GUARD) == b"cancelled\n"
        assert markers.read() == b""
        completed = True
    finally:
        finish(process, terminal, markers, completed=completed)
