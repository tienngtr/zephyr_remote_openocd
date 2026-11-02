# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from tests.acceptance.log_file import LogFileAcceptance
from tests.hardware_support import DebugServerFixture, FlashFixture, SemihostingFixture

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealLogFile:
    def test_stalled_log_consumer_does_not_stop_flash(
        self, flash_fixture: FlashFixture, tmp_path: Path
    ) -> None:
        LogFileAcceptance().stalled_flash(flash_fixture, tmp_path)

    def test_stalled_log_consumer_does_not_stop_live_server(
        self, debugserver_fixture: DebugServerFixture, tmp_path: Path
    ) -> None:
        LogFileAcceptance().stalled_debugserver(debugserver_fixture, tmp_path)

    def test_semihosting_console_stays_on_stdout_with_log_file(
        self, semihosting_fixture: SemihostingFixture, tmp_path: Path
    ) -> None:
        LogFileAcceptance().semihosting_stays_on_stdout(semihosting_fixture, tmp_path)
