# SPDX-License-Identifier: Apache-2.0

from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

from tests.hardware.test_real_flash import TestRealOpenOcdFlash as _FlashTest
from tests.hardware.test_real_semihosting import TestRealSemihosting as _SemihostingTest


def test_flash_uses_one_serial_reader_for_selected_image_output() -> None:
    fixture = MagicMock()
    fixture.operation.serial.timeout = 30
    fixture.operation.output_patterns = ()
    reader = MagicMock()
    reader.wait.return_value = 0
    ssh = MagicMock()
    ssh.popen.return_value = reader
    flashes = [CompletedProcess([], 0, ""), CompletedProcess([], 0, "")]

    with (
        patch("tests.hardware.test_real_flash.SshCommand", return_value=ssh),
        patch.object(_FlashTest, "_flash", side_effect=flashes),
        patch.object(_FlashTest, "_reader_command", return_value="serial-reader"),
        patch.object(_FlashTest, "_assert_bindto"),
        patch(
            "tests.hardware.test_real_flash._read_event",
            side_effect=(
                {"type": "READY"},
                {"type": "ARMED"},
                {"type": "MATCH", "data": ""},
            ),
        ),
        patch("tests.hardware.test_real_flash._stop"),
    ):
        _FlashTest().test_configured_target_flashes_and_emits_fresh_serial_output(fixture)

    ssh.popen.assert_called_once()


def test_semihosting_uses_debug_as_the_only_programming_command() -> None:
    fixture = MagicMock()
    fixture.operation.gdb_commands = ("quit",)
    fixture.operation.timeout = 30
    fixture.operation.output = "output"
    fixture.target.workspace = "/workspace"
    process = MagicMock()
    process.communicate.return_value = (b"output", None)
    process.poll.return_value = 0
    process.returncode = 0
    process.stdin = None
    process.stdout = None
    process.stderr = None

    with (
        patch.object(_SemihostingTest, "_west", return_value=["west", "debug"]),
        patch.object(_SemihostingTest, "_environment", return_value={}),
        patch("tests.hardware.test_real_semihosting.subprocess.run") as run,
        patch("tests.hardware.test_real_semihosting.subprocess.Popen", return_value=process),
        patch("tests.hardware.test_real_semihosting.assert_semihosting_acceptance"),
    ):
        _SemihostingTest().test_direct_semihosting_console_normal_completion(fixture)

    run.assert_not_called()
