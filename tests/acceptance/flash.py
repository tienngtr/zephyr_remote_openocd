# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ipaddress
import re

import pytest
from zephyr_remote_openocd.controller._ssh import SshCommand

from tests.acceptance.firmware import establish_precondition, west_flash
from tests.hardware_support import FlashFixture
from tests.process_support import cleanup_on_exit
from tests.serial_reader import captured_text, endpoint_reader_command
from tests.serial_reader import read_event as _read_event
from tests.serial_reader import stop_reader as _stop


class FlashAcceptance:
    def run(self, flash_fixture: FlashFixture) -> None:
        fixture = flash_fixture
        ssh = SshCommand(fixture.target.host.ssh_command)
        establish_precondition(fixture.target, fixture.precondition)

        observation = fixture.operation.serial
        remote_command = endpoint_reader_command(
            fixture.serial, observation.pattern, observation.timeout + 180
        )
        reader = ssh.popen(fixture.target.host.ssh_host, remote_command)
        with cleanup_on_exit(lambda: _stop(reader)):
            assert _read_event(reader, 15)["type"] == "READY"
            assert reader.stdin is not None
            reader.stdin.write(b"ARM\n")
            reader.stdin.flush()
            assert _read_event(reader, 15)["type"] == "ARMED"
            flash = west_flash(fixture.target, fixture.target.build_dir)
            try:
                event = _read_event(reader, observation.timeout + 2)
            except AssertionError:
                pytest.fail(f"serial reader failed after flash:\n{flash.stdout}", pytrace=False)
            captured = captured_text(event)
            assert flash.returncode == 0, flash.stdout + "\nserial:\n" + captured
            assert event["type"] == "MATCH", f"serial oracle failed: {event}\n{captured}"
            self._assert_bindto(fixture, flash.stdout)
            for pattern in fixture.operation.output_patterns:
                assert re.search(pattern, flash.stdout)
            assert reader.wait(timeout=5) == 0

    @staticmethod
    def _assert_bindto(fixture: FlashFixture, output: str) -> None:
        if not fixture.operation.assert_bindto:
            return
        match = re.search(r"bindto name: (\S+)", output)
        assert match is not None, output
        address = ipaddress.ip_address(match.group(1))
        assert address in ipaddress.ip_network("127.64.0.0/10")
