# SPDX-License-Identifier: Apache-2.0

"""Exercise terminal-style interrupts without signalling pytest's group."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote import ssh

pytestmark = [pytest.mark.local, pytest.mark.skipif(os.name != "posix", reason="POSIX signals")]


PROCESS_GROUP_SCENARIO = r'''
import os
import select
import signal
import subprocess
import sys

from zephyr_remote_openocd.remote.ssh import SshCommand, _stop_process


def read_ready(stream):
    assert select.select([stream], [], [], 30)[0], "child did not respond"
    return stream.readline()


transport_code = """
import signal
import sys
signal.signal(signal.SIGINT, signal.SIG_DFL)
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
    assert read_ready(transport.stdout) == b"ready\n"
    # Zephyr run_client ignores SIGINT in the runner after starting its server.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    client = subprocess.Popen(
        [sys.executable, "-c", client_code], stdout=subprocess.PIPE
    )
    assert read_ready(client.stdout) == b"ready\n"
    os.killpg(os.getpgrp(), signal.SIGINT)
    assert client.wait(timeout=30) == -signal.SIGINT
    os.write(transport.stdin.fileno(), b"ping\n")
    assert read_ready(transport.stdout) == b"alive\n", "SSH transport lost to SIGINT"
    assert transport.poll() is None
finally:
    if client is not None:
        if client.poll() is None:
            client.kill()
        client.wait(timeout=30)
        client.stdout.close()
    _stop_process(transport)

assert transport.returncode == -signal.SIGTERM
assert transport.stdin.closed
assert transport.stdout.closed
'''


def test_long_lived_ssh_survives_client_process_group_sigint(tmp_path: Path) -> None:
    scenario = tmp_path / "process_group.py"
    scenario.write_text(PROCESS_GROUP_SCENARIO, encoding="utf-8")
    environment = os.environ.copy()
    production_root = str(Path(ssh.__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (production_root, environment.get("PYTHONPATH")))
    )
    result = subprocess.run(
        [sys.executable, str(scenario)],
        start_new_session=True,
        capture_output=True,
        env=environment,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
