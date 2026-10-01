# SPDX-License-Identifier: Apache-2.0

"""Exercise terminal-style interrupts without signalling pytest's group."""

from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote import ssh

from tests.process_support import read_line

pytestmark = [pytest.mark.local, pytest.mark.skipif(os.name != "posix", reason="POSIX signals")]


PROCESS_GROUP_SCENARIO = r'''
import fcntl
import os
import signal
import subprocess
import sys
import termios

from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.process_support import read_line


# Acquire a real controlling terminal in this isolated session. The outer
# test supplies both prompt input and terminal-generated Ctrl-C through it.
terminal = int(sys.argv[1])
fcntl.ioctl(terminal, termios.TIOCSCTTY, 0)
os.tcsetpgrp(terminal, os.getpgrp())
attributes = termios.tcgetattr(terminal)
attributes[3] |= termios.ISIG | termios.ICANON
attributes[3] &= ~termios.ECHO
attributes[6][termios.VINTR] = b"\x03"
termios.tcsetattr(terminal, termios.TCSANOW, attributes)
os.close(terminal)


transport_code = """
import os
import signal
import sys
signal.signal(signal.SIGINT, signal.SIG_DFL)
with open("/dev/tty", "r+b", buffering=0) as terminal:
    assert os.tcgetpgrp(terminal.fileno()) == os.getpgrp()
    terminal.write(b"auth-prompt\\n")
    assert terminal.readline() == b"auth-response\\n"
print("ready", flush=True)
for line in sys.stdin:
    print("alive", flush=True)
"""
client_code = """
import signal
signal.signal(signal.SIGINT, signal.SIG_DFL)
print("ready", flush=True)
signal.pause()
"""

transport = SshCommand((sys.executable, "-c", transport_code)).popen("host", "serve")
client = None
try:
    assert read_line(transport.stdout) == b"ready\n", transport.stderr_tail()
    # Zephyr run_client ignores SIGINT in the runner after starting its server.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    client = subprocess.Popen(
        [sys.executable, "-c", client_code], stdout=subprocess.PIPE
    )
    assert read_line(client.stdout) == b"ready\n"
    print("interrupt-ready", flush=True)
    assert client.wait(timeout=30) == -signal.SIGINT
    os.write(transport.stdin.fileno(), b"ping\n")
    assert read_line(transport.stdout) == b"alive\n", "SSH transport lost to SIGINT"
    assert transport.poll() is None
finally:
    if client is not None:
        if client.poll() is None:
            client.kill()
        client.wait(timeout=30)
        client.stdout.close()
    transport.terminate()
    transport.wait(timeout=30)
    transport.close_stderr()
    transport.stdin.close()
    transport.stdout.close()

assert transport.returncode is not None
assert transport.stdin.closed
assert transport.stdout.closed
'''


def test_long_lived_ssh_preserves_terminal_prompts_and_survives_ctrl_c(tmp_path: Path) -> None:
    scenario = tmp_path / "process_group.py"
    scenario.write_text(PROCESS_GROUP_SCENARIO, encoding="utf-8")
    environment = os.environ.copy()
    production_root = str(Path(ssh.__file__).resolve().parents[2])
    repository_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (production_root, repository_root, environment.get("PYTHONPATH")))
    )
    master_fd, slave_fd = os.openpty()
    try:
        with (
            os.fdopen(master_fd, "r+b", buffering=0) as terminal,
            subprocess.Popen(
                [sys.executable, str(scenario), str(slave_fd)],
                start_new_session=True,
                pass_fds=(slave_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            ) as process,
        ):
            try:
                assert process.stdout is not None
                ready, _, _ = select.select([terminal, process.stdout], [], [], 30)
                if process.stdout in ready:
                    _, errors = process.communicate(timeout=30)
                    pytest.fail(errors.decode("utf-8", "replace"))
                assert terminal in ready, "SSH terminal prompt did not appear"
                assert read_line(terminal).strip() == b"auth-prompt"
                terminal.write(b"auth-response\n")
                assert read_line(process.stdout) == b"interrupt-ready\n"
                terminal.write(b"\x03")
                _, errors = process.communicate(timeout=90)
                assert process.returncode == 0, errors.decode("utf-8", "replace")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=30)
    finally:
        os.close(slave_fd)
