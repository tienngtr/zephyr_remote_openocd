# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from unittest.mock import patch

from zephyr_remote_openocd.remote.ssh import SshCommand


def test_fixed_arguments_are_preserved_without_a_shell():
    ssh = SshCommand(("ssh", "-F", "/a file", "-o", "BatchMode=yes"))
    assert ssh.argv("board-lab", "printf marker") == [
        "ssh",
        "-F",
        "/a file",
        "-o",
        "BatchMode=yes",
        "board-lab",
        "printf marker",
    ]


@patch("subprocess.run")
def test_stream_uses_stdin(run):
    run.return_value = subprocess.CompletedProcess([], 0, b"ok", b"")
    result = SshCommand(("ssh", "-p", "2222")).run("host", "consume", input_data=b"payload")
    assert result.stdout == b"ok"
    run.assert_called_once_with(
        ["ssh", "-p", "2222", "host", "consume"],
        input=b"payload",
        capture_output=True,
        check=False,
        timeout=15,
    )


@patch("subprocess.Popen")
def test_long_lived_process_preserves_fixed_arguments(popen):
    SshCommand(("ssh", "-F", "/a file")).popen("host", "serve", "-o", "ControlMaster=no")
    popen.assert_called_once_with(
        ["ssh", "-F", "/a file", "-o", "ControlMaster=no", "host", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
