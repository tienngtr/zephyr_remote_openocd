# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.controller._forwarding import ForwardManager
from zephyr_remote_openocd.controller._ssh import SshCommand
from zephyr_remote_openocd.controller.authority import SessionError
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec

pytestmark = pytest.mark.local


@pytest.mark.parametrize(
    "fixed_args",
    (
        (),
        ("-o", "ExitOnForwardFailure=no", "-o", "ClearAllForwardings=yes"),
        ("-oexitonforwardfailure=no", "-oCLEARALLFORWARDINGS=yes"),
        ("-o", "ExitOnForwardFailure no", "-o", "ClearAllForwardings yes"),
    ),
)
def test_forward_manager_preserves_effective_forwarding_requirements(
    tmp_path: Path, fixed_args: tuple[str, ...]
) -> None:
    executable = shutil.which("ssh")
    if executable is None:
        pytest.skip("OpenSSH client is not installed")
    # Exercise the configured executable path without assuming its basename.
    alternate = tmp_path / "custom-ssh"
    alternate.symlink_to(executable)
    config = tmp_path / "ssh_config"
    config.write_text(
        "Host *\n    ExitOnForwardFailure no\n    ClearAllForwardings yes\n    ConnectTimeout 17\n",
        encoding="utf-8",
    )
    ssh = SshCommand((str(alternate), "-F", str(config), *fixed_args))
    forward = ForwardSpec("gdb", 32100, 3333)
    manager = ForwardManager(ssh, "example.invalid")
    # Capture the actual subprocess argv from the real manager and transport.
    # No process is acquired, so no lifecycle double or readiness bypass is needed.
    with (
        patch(
            "subprocess.Popen.__init__", autospec=True, side_effect=OSError("capture argv")
        ) as popen,
        pytest.raises(SessionError),
    ):
        manager.start((forward,), "127.64.0.1")
    argv = popen.call_args.args[1]
    result = subprocess.run(
        [argv[0], "-G", *argv[1:]],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    options = dict(line.split(" ", 1) for line in result.stdout.splitlines())
    assert options["exitonforwardfailure"] == "yes"
    assert options["clearallforwardings"] == "no"
    endpoints = [
        endpoint.replace("[", "").replace("]", "") for endpoint in options["localforward"].split()
    ]
    assert endpoints == ["127.0.0.1:32100", "127.64.0.1:3333"]
    assert options["connecttimeout"] == "17"
