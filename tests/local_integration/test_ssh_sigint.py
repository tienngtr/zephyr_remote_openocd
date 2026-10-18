# SPDX-License-Identifier: Apache-2.0

"""Exercise terminal prompts and interrupts with inherited SIGINT blocking.

The synthetic transport retains the inherited mask. This validates the launch
mechanism under that assumption, not survival of every compatible SSH client.
The scenario is isolated so terminal interrupts cannot signal pytest's group.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.controller import _ssh as ssh

from tests.process_support import read_line
from tests.support import ROOT

pytestmark = [pytest.mark.local, pytest.mark.skipif(os.name != "posix", reason="POSIX signals")]


SCENARIO = ROOT / "tests/fixtures/ssh_sigint.py"


def test_long_lived_ssh_preserves_terminal_prompts_and_survives_ctrl_c() -> None:
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
                [sys.executable, str(SCENARIO), "scenario", str(slave_fd)],
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
