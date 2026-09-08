# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import subprocess

import pytest
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.support import ROOT

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealOpenOcdFlash:
    def test_configured_target_flashes(self, flash_fixture):
        self._run_fixture(flash_fixture)

    def _run_fixture(self, fixture):
        required = (
            "ssh_command",
            "host",
            "build_dir",
            "config_path",
        )
        missing = [key for key in required if key not in fixture]
        if missing:
            pytest.fail(f"fixture {fixture.get('id')} is missing: {', '.join(missing)}")
        ssh = SshCommand(tuple(fixture["ssh_command"]))
        west = fixture.get("west") or shutil.which("west")
        if not west:
            pytest.fail("fixture has no west executable and west is not on PATH")
        command = [
            str(west),
            "flash",
            "-d",
            str(fixture["build_dir"]),
            "-r",
            "remote_openocd",
            "--no-rebuild",
        ]
        runner_args = fixture.get("runner_args", [])
        if runner_args:
            command.extend(("--", *map(str, runner_args)))
        environment = os.environ.copy()
        environment.pop("ZEPHYR_REMOTE_OPENOCD_RECORD", None)
        environment.update(
            {
                "EXTRA_ZEPHYR_MODULES": str(ROOT),
                "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(fixture["config_path"]),
            }
        )
        environment.update(
            {str(key): str(value) for key, value in fixture.get("environment", {}).items()}
        )
        flash = subprocess.run(
            command,
            cwd=fixture.get("workspace"),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        assert flash.returncode == 0, flash.stdout
        session = re.search(
            r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)", flash.stdout
        )
        assert session is not None, flash.stdout
        address = ipaddress.ip_address(session.group(3))
        assert address in ipaddress.ip_network("127.64.0.0/10")
        if fixture.get("assert_openocd_bindto"):
            assert f"bindto name: {address}" in flash.stdout
        cleanup = ssh.run(fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20)
        assert cleanup.returncode == 0, cleanup.stderr.decode("utf-8", "replace")
        for pattern in fixture.get("expected_flash_patterns", []):
            assert re.search(pattern, flash.stdout)
