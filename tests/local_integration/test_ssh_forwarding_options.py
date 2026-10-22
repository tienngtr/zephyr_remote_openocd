# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

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
    # The configured client records the argv it is executed with, so capture
    # happens at the real exec boundary after the production launcher.
    recorded = tmp_path / "argv.json"
    alternate = tmp_path / "custom-ssh"
    alternate.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(recorded)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "sys.exit(255)\n"
    )
    alternate.chmod(0o755)
    config = tmp_path / "ssh_config"
    config.write_text(
        "Host *\n    ExitOnForwardFailure no\n    ClearAllForwardings yes\n    ConnectTimeout 17\n",
        encoding="utf-8",
    )
    ssh = SshCommand((str(alternate), "-F", str(config), *fixed_args))
    forward = ForwardSpec("gdb", 32100, 3333)
    manager = ForwardManager(ssh, "example.invalid")
    with pytest.raises(SessionError):
        manager.start((forward,), "127.64.0.1")
    # Real OpenSSH evaluates the exact arguments the configured client received.
    arguments = json.loads(recorded.read_text(encoding="utf-8"))
    result = subprocess.run(
        [executable, "-G", *arguments],
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
