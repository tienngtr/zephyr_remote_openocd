# SPDX-License-Identifier: Apache-2.0

"""Terminal Ctrl-C ownership across a configured GDB wrapper and GDB.

The ``scenario`` role mirrors Zephyr's ``run_client()`` inside the interactive
scope with a PTY as its controlling terminal. The ``wrapper`` role is the
configured GDB wrapper, and ``inferior`` is the program GDB runs.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import termios
import time

from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.waiting import wait_for


def run_client(argv: list[str]) -> None:
    # Zephyr 4.4 run_client(): ignore SIGINT around check_call().
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        subprocess.check_call(argv)
    finally:
        signal.signal(signal.SIGINT, previous)


def scenario(terminal: int, markers: int, mode: str, client: list[str]) -> None:
    fcntl.ioctl(terminal, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(terminal, os.getpgrp())
    attributes = termios.tcgetattr(terminal)
    attributes[3] |= termios.ISIG
    attributes[6][termios.VINTR] = b"\x03"
    termios.tcsetattr(terminal, termios.TCSANOW, attributes)
    for descriptor in (0, 1, 2):
        os.dup2(terminal, descriptor)
    os.close(terminal)

    def report(message: str) -> None:
        os.write(markers, (message + "\n").encode())

    try:
        with operation_scope() as operation:
            report(f"capturing={operation.capturing}")
            if mode == "latched":
                signal.raise_signal(signal.SIGINT)
            with operation.interactive_client():
                report("client-launched")
                run_client(client)
            report(f"restored requested={operation.requested}")
            # Runner-owned Ctrl-C after restoration ends this ordinary wait.
            wait_for()
    except OperationCancelled:
        report("cancelled")


def wrapper(argv: list[str]) -> None:
    disposition = signal.getsignal(signal.SIGINT)
    state = "ignored" if disposition == signal.SIG_IGN else str(disposition)
    os.write(1, f"wrapper-sigint={state}\n".encode())
    os.execvp(argv[0], argv)


def inferior() -> None:
    os.write(1, b"inferior-ready\n")
    time.sleep(3600)


def main() -> None:
    role, *arguments = sys.argv[1:]
    if role == "wrapper":
        wrapper(arguments)
    elif role == "inferior":
        inferior()
    else:
        assert role == "scenario"
        terminal, markers, mode, *client = arguments
        scenario(int(terminal), int(markers), mode, client)


if __name__ == "__main__":
    main()
