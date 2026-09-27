# SPDX-License-Identifier: Apache-2.0

from unittest.mock import MagicMock, patch

from tests.hardware.test_real_semihosting import TestRealSemihosting as _SemihostingTest


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
