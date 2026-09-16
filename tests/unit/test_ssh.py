# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import patch

from zephyr_remote_openocd.remote.ssh import SshCommand


def test_fixed_arguments_are_preserved_without_a_shell():
    ssh = SshCommand(("custom-ssh", "-F", "/a file", "-o", "BatchMode=yes"))
    assert ssh.argv("board-lab", "printf marker") == [
        "custom-ssh",
        "-F",
        "/a file",
        "-o",
        "BatchMode=yes",
        "board-lab",
        "printf marker",
    ]


@patch("subprocess.Popen")
def test_long_lived_process_preserves_explicit_path_and_generated_arguments(popen):
    SshCommand(("/opt/client/custom-ssh", "-F", "/a file")).popen("host", "serve", "-N")
    popen.assert_called_once_with(
        ["/opt/client/custom-ssh", "-F", "/a file", "-N", "host", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_bare_alternate_executable_is_resolved_through_path(tmp_path, monkeypatch):
    executable = tmp_path / "custom-ssh"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    result = SshCommand(("custom-ssh", "--fixed")).run("target", "remote command")

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--fixed", "target", "remote command"]
