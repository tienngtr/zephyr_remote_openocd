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

from tests.serial_reader import read_event as _read_event
from tests.serial_reader import remote_serial_reader_command
from tests.serial_reader import stop_reader as _stop
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
            "precondition_build_dir",
            "config_path",
            "serial_device",
            "serial_baud",
            "expected_pattern",
            "serial_timeout",
            "quiescence_timeout",
        )
        missing = [key for key in required if key not in fixture]
        if missing:
            pytest.fail(f"fixture {fixture.get('id')} is missing: {', '.join(missing)}")
        ssh = SshCommand(tuple(fixture["ssh_command"]))
        precondition = self._flash(fixture, fixture["precondition_build_dir"])
        assert precondition.returncode == 0, precondition.stdout
        self._assert_session(fixture, ssh, precondition.stdout)

        quiet_reader = ssh.popen(
            fixture["host"],
            self._reader_command(fixture, float(fixture["quiescence_timeout"])),
            "-o",
            "ControlMaster=no",
        )
        try:
            assert _read_event(quiet_reader, 15)["type"] == "READY"
            assert quiet_reader.stdin is not None
            quiet_reader.stdin.write(b"ARM\n")
            quiet_reader.stdin.flush()
            assert _read_event(quiet_reader, 15)["type"] == "ARMED"
            quiet = _read_event(quiet_reader, float(fixture["quiescence_timeout"]) + 2)
            captured = base64.b64decode(quiet.get("data", "")).decode("utf-8", "replace")
            assert quiet["type"] == "TIMEOUT", (
                "precondition image emitted the intended image marker:\n" + captured
            )
            assert quiet_reader.wait(timeout=5) == 2
        finally:
            _stop(quiet_reader)

        remote_command = remote_serial_reader_command(
            str(fixture["serial_device"]),
            int(fixture["serial_baud"]),
            str(fixture["expected_pattern"]),
            float(fixture["serial_timeout"]) + 180,
            data_bits=int(fixture.get("serial_data_bits", 8)),
            parity=str(fixture.get("serial_parity", "none")),
            stop_bits=int(fixture.get("serial_stop_bits", 1)),
            flow_control=str(fixture.get("serial_flow_control", "none")),
        )
        reader = ssh.popen(fixture["host"], remote_command, "-o", "ControlMaster=no")
        try:
            assert _read_event(reader, 15)["type"] == "READY"
            assert reader.stdin is not None
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()
            assert _read_event(reader, 15)["type"] == "ARMED"
            flash = self._flash(fixture, fixture["build_dir"])
            try:
                event = _read_event(reader, float(fixture["serial_timeout"]) + 2)
            except AssertionError:
                pytest.fail(f"serial reader failed after flash:\n{flash.stdout}", pytrace=False)
            captured = base64.b64decode(event.get("data", "")).decode("utf-8", "replace")
            assert flash.returncode == 0, flash.stdout + "\nserial:\n" + captured
            assert event["type"] == "MATCH", f"serial oracle failed: {event}\n{captured}"
            self._assert_session(fixture, ssh, flash.stdout, check_bind=True)
            for pattern in fixture.get("expected_flash_patterns", []):
                assert re.search(pattern, flash.stdout)
            assert reader.wait(timeout=5) == 0
        finally:
            _stop(reader)

    @staticmethod
    def _reader_command(fixture, timeout):
        return remote_serial_reader_command(
            str(fixture["serial_device"]),
            int(fixture["serial_baud"]),
            str(fixture["expected_pattern"]),
            timeout,
            data_bits=int(fixture.get("serial_data_bits", 8)),
            parity=str(fixture.get("serial_parity", "none")),
            stop_bits=int(fixture.get("serial_stop_bits", 1)),
            flow_control=str(fixture.get("serial_flow_control", "none")),
        )

    @staticmethod
    def _flash(fixture, build_dir):
        west = fixture.get("west") or shutil.which("west")
        if not west:
            pytest.fail("fixture has no west executable and west is not on PATH")
        command = [
            str(west),
            "flash",
            "-d",
            str(build_dir),
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
        return subprocess.run(
            command,
            cwd=fixture.get("workspace"),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )

    @staticmethod
    def _assert_session(fixture, ssh, output, *, check_bind=False):
        session = re.search(r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)", output)
        assert session is not None, output
        address = ipaddress.ip_address(session.group(3))
        assert address in ipaddress.ip_network("127.64.0.0/10")
        if check_bind and fixture.get("assert_openocd_bindto"):
            assert f"bindto name: {address}" in output
        cleanup = ssh.run(fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20)
        assert cleanup.returncode == 0, cleanup.stderr.decode("utf-8", "replace")
