# SPDX-License-Identifier: Apache-2.0

"""Long-lived SSH terminal prompt and Ctrl-C survival in an isolated session.

The ``scenario`` role acquires a real controlling terminal from its argument
descriptor. The outer test supplies prompt input and terminal-generated Ctrl-C
through it. The ``transport`` and ``client`` roles are the synthetic SSH
transport and the Zephyr-style client the scenario launches.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import termios

from zephyr_remote_openocd.controller._ssh import SshCommand

from tests.process_support import read_line


def transport() -> None:
    # Reset disposition, but intentionally retain inherited SIGINT blocking.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    with open("/dev/tty", "r+b", buffering=0) as terminal:
        assert os.tcgetpgrp(terminal.fileno()) == os.getpgrp()
        terminal.write(b"auth-prompt\n")
        assert terminal.readline() == b"auth-response\n"
    print("ready", flush=True)
    for _line in sys.stdin:
        print("alive", flush=True)


def client() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    print("ready", flush=True)
    signal.pause()


def scenario(terminal: int) -> None:
    fcntl.ioctl(terminal, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(terminal, os.getpgrp())
    attributes = termios.tcgetattr(terminal)
    attributes[3] |= termios.ISIG | termios.ICANON
    attributes[3] &= ~termios.ECHO
    attributes[6][termios.VINTR] = b"\x03"
    termios.tcsetattr(terminal, termios.TCSANOW, attributes)
    os.close(terminal)

    managed = SshCommand((sys.executable, __file__, "transport")).popen("host", "serve")
    launched: subprocess.Popen[bytes] | None = None
    assert managed.stdin is not None and managed.stdout is not None
    try:
        assert read_line(managed.stdout) == b"ready\n", managed.stderr_tail()
        # Zephyr run_client ignores SIGINT in the runner after starting its server.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        launched = subprocess.Popen([sys.executable, __file__, "client"], stdout=subprocess.PIPE)
        assert launched.stdout is not None
        assert read_line(launched.stdout) == b"ready\n"
        print("interrupt-ready", flush=True)
        assert launched.wait(timeout=30) == -signal.SIGINT
        os.write(managed.stdin.fileno(), b"ping\n")
        assert read_line(managed.stdout) == b"alive\n", "SSH transport lost to SIGINT"
        assert managed.poll() is None
    finally:
        if launched is not None:
            if launched.poll() is None:
                launched.kill()
            launched.wait(timeout=30)
            assert launched.stdout is not None
            launched.stdout.close()
        managed.terminate()
        managed.wait(timeout=30)
        managed.close_stderr()
        managed.stdin.close()
        managed.stdout.close()

    assert managed.returncode is not None
    assert managed.stdin.closed
    assert managed.stdout.closed


def main() -> None:
    role = sys.argv[1]
    if role == "transport":
        transport()
    elif role == "client":
        client()
    else:
        assert role == "scenario"
        scenario(int(sys.argv[2]))


if __name__ == "__main__":
    main()
