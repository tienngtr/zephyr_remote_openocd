# SPDX-License-Identifier: Apache-2.0

import pytest

from tests.acceptance.flash import FlashAcceptance
from tests.hardware_support import FlashFixture

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealOpenOcdFlash:
    def test_configured_target_flashes_and_emits_fresh_serial_output(
        self, flash_fixture: FlashFixture
    ) -> None:
        FlashAcceptance().run(flash_fixture)
