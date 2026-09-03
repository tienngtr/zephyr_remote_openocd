# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shlex
import shutil
import subprocess

import pytest
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.serial_reader import (
    read_event as _read_event,
)
from tests.serial_reader import (
    remote_serial_reader_command,
)
from tests.serial_reader import (
    stop_reader as _stop,
)
from tests.support import ROOT

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealOpenOcdFlash:
    def test_configured_target_flashes_and_emits_fresh_serial_output(self, flash_fixture):
        self._run_fixture(flash_fixture)

    def _run_fixture(self, fixture):
        required = (
            "ssh_command",
            "host",
            "build_dir",
            "config_path",
            "serial_device",
            "serial_baud",
            "expected_pattern",
            "serial_timeout",
        )
        missing = [key for key in required if key not in fixture]
        if missing:
            pytest.fail(f"fixture {fixture.get('id')} is missing: {', '.join(missing)}")
        ssh = SshCommand(tuple(fixture["ssh_command"]))
        remote_command = remote_serial_reader_command(
            str(fixture["serial_device"]),
            int(fixture["serial_baud"]),
            str(fixture["expected_pattern"]),
            float(fixture["serial_timeout"]),
            data_bits=int(fixture.get("serial_data_bits", 8)),
            parity=str(fixture.get("serial_parity", "none")),
            stop_bits=int(fixture.get("serial_stop_bits", 1)),
            flow_control=str(fixture.get("serial_flow_control", "none")),
        )
        reader = ssh.popen(fixture["host"], remote_command, "-o", "ControlMaster=no")
        try:
            ready = _read_event(reader, 15)
            assert ready["type"] == "READY", ready
            assert reader.stdin is not None
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()

            west = fixture.get("west") or shutil.which("west")
            if not west:
                pytest.fail("fixture has no west executable and west is not on PATH")
            command = [
                str(west),
                "flash",
                "-d",
                str(fixture["build_dir"]),
                "-r",
                "remote-openocd",
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
                timeout=float(fixture["serial_timeout"]) + 180,
            )
            event = _read_event(reader, float(fixture["serial_timeout"]) + 2)
            captured = base64.b64decode(event.get("data", "")).decode("utf-8", "replace")
            assert flash.returncode == 0, flash.stdout + "\nserial:\n" + captured
            assert event["type"] == "MATCH", f"serial oracle failed: {event}\n{captured}"
            session = re.search(
                r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)", flash.stdout
            )
            assert session is not None, flash.stdout
            assert session is not None
            address = ipaddress.ip_address(session.group(3))
            assert address in ipaddress.ip_network("127.64.0.0/10")
            if fixture.get("assert_openocd_bindto"):
                assert f"bindto name: {address}" in flash.stdout
            cleanup = ssh.run(
                fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20
            )
            assert cleanup.returncode == 0, cleanup.stderr.decode("utf-8", "replace")
            for pattern in fixture.get("expected_flash_patterns", []):
                assert re.search(pattern, flash.stdout)
            assert reader.wait(timeout=5) == 0
        finally:
            _stop(reader)
