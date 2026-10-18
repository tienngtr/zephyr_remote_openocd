# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import pytest

from tests.acceptance.rtt import RttAcceptance
from tests.hardware_support import RttFixture

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealRtt:
    def test_standalone_rtt(self, rtt_fixture: RttFixture) -> None:
        RttAcceptance().standalone(rtt_fixture)

    @pytest.mark.parametrize("command", ("debug", "attach"))
    def test_gdb_rtt_server_keeps_gdb_active(
        self, rtt_fixture: RttFixture, tmp_path: Path, command: str
    ) -> None:
        RttAcceptance().gdb_server(rtt_fixture, tmp_path, command)

    def test_debugserver_serves_gdb_and_rtt_concurrently(
        self, rtt_fixture: RttFixture, tmp_path: Path
    ) -> None:
        RttAcceptance().debugserver(rtt_fixture, tmp_path)
