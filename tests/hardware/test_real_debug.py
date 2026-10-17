# SPDX-License-Identifier: Apache-2.0

import pytest

from tests.acceptance.debug import DebugAcceptance
from tests.hardware_support import (
    AttachFixture,
    DebugFixture,
    DebugServerFixture,
    ThreadInfoFixture,
)

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealOpenOcdDebug:
    def test_debug(self, debug_fixture: DebugFixture) -> None:
        DebugAcceptance().debug(debug_fixture)

    def test_attach(self, attach_fixture: AttachFixture) -> None:
        DebugAcceptance().attach(attach_fixture)

    def test_debugserver(self, debugserver_fixture: DebugServerFixture, tmp_path) -> None:
        DebugAcceptance().debugserver(debugserver_fixture, tmp_path / "debugserver.log")

    def test_thread_info_on_capable_fixture(
        self, thread_info_fixture: ThreadInfoFixture, tmp_path
    ) -> None:
        DebugAcceptance().thread_info(thread_info_fixture, tmp_path / "threads.log")
