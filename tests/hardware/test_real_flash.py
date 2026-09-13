# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import base64
import ipaddress
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.hardware_support import FlashFixture
from tests.serial_reader import read_event as _read_event
from tests.serial_reader import remote_serial_reader_command
from tests.serial_reader import stop_reader as _stop
from tests.support import ROOT

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealOpenOcdFlash:
    def test_configured_target_flashes_and_emits_fresh_serial_output(
        self, flash_fixture: FlashFixture
    ) -> None:
        fixture = flash_fixture
        ssh = SshCommand(fixture.target.host.ssh_command)
        precondition = self._flash(fixture, fixture.precondition_build_dir)
        assert precondition.returncode == 0, precondition.stdout
        self._assert_session(fixture, ssh, precondition.stdout)

        quiet_reader = ssh.popen(
            fixture.target.host.ssh_host,
            self._reader_command(fixture, fixture.operation.quiescence_timeout),
        )
        try:
            assert _read_event(quiet_reader, 15)["type"] == "READY"
            assert quiet_reader.stdin is not None
            quiet_reader.stdin.write(b"ARM\n")
            quiet_reader.stdin.flush()
            assert _read_event(quiet_reader, 15)["type"] == "ARMED"
            quiet = _read_event(quiet_reader, fixture.operation.quiescence_timeout + 2)
            captured = self._captured_text(quiet)
            assert quiet["type"] == "TIMEOUT", (
                "precondition image emitted the intended image marker:\n" + captured
            )
            assert quiet_reader.wait(timeout=5) == 2
        finally:
            _stop(quiet_reader)

        observation = fixture.operation.serial
        remote_command = self._reader_command(fixture, observation.timeout + 180)
        reader = ssh.popen(fixture.target.host.ssh_host, remote_command)
        try:
            assert _read_event(reader, 15)["type"] == "READY"
            assert reader.stdin is not None
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()
            assert _read_event(reader, 15)["type"] == "ARMED"
            flash = self._flash(fixture, fixture.target.build_dir)
            try:
                event = _read_event(reader, observation.timeout + 2)
            except AssertionError:
                pytest.fail(f"serial reader failed after flash:\n{flash.stdout}", pytrace=False)
            captured = self._captured_text(event)
            assert flash.returncode == 0, flash.stdout + "\nserial:\n" + captured
            assert event["type"] == "MATCH", f"serial oracle failed: {event}\n{captured}"
            self._assert_session(fixture, ssh, flash.stdout, check_bind=True)
            for pattern in fixture.operation.output_patterns:
                assert re.search(pattern, flash.stdout)
            assert reader.wait(timeout=5) == 0
        finally:
            _stop(reader)

    @staticmethod
    def _reader_command(fixture: FlashFixture, timeout: float) -> str:
        endpoint = fixture.serial
        return remote_serial_reader_command(
            endpoint.device,
            endpoint.baud,
            fixture.operation.serial.pattern,
            timeout,
            data_bits=endpoint.data_bits,
            parity=endpoint.parity,
            stop_bits=endpoint.stop_bits,
            flow_control=endpoint.flow_control,
        )

    @staticmethod
    def _captured_text(event: dict[str, object]) -> str:
        encoded = event.get("data", "")
        assert isinstance(encoded, str), f"invalid serial reader event: {event}"
        return base64.b64decode(encoded).decode("utf-8", "replace")

    @staticmethod
    def _flash(fixture: FlashFixture, build_dir: Path) -> subprocess.CompletedProcess[str]:
        target = fixture.target
        command = [
            str(target.build_environment.west),
            "flash",
            "-d",
            str(build_dir),
            "-r",
            "remote_openocd",
            "--no-rebuild",
        ]
        if target.runner_args:
            command.extend(("--", *target.runner_args))
        environment = os.environ.copy()
        environment.pop("ZRO_RECORD", None)
        environment.update(
            EXTRA_ZEPHYR_MODULES=str(ROOT),
            ZEPHYR_REMOTE_OPENOCD_CONFIG=str(target.config_path),
        )
        environment.update(dict(target.environment))
        return subprocess.run(
            command,
            cwd=target.workspace,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )

    @staticmethod
    def _assert_session(
        fixture: FlashFixture,
        ssh: SshCommand,
        output: str,
        *,
        check_bind: bool = False,
    ) -> None:
        session = re.search(r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)", output)
        assert session is not None, output
        address = ipaddress.ip_address(session.group(3))
        assert address in ipaddress.ip_network("127.64.0.0/10")
        if check_bind and fixture.operation.assert_bindto:
            assert f"bindto name: {address}" in output
        cleanup = ssh.run(
            fixture.target.host.ssh_host,
            f"test ! -e {shlex.quote(session.group(2))}",
            timeout=20,
        )
        assert cleanup.returncode == 0, cleanup.stderr.decode("utf-8", "replace")
