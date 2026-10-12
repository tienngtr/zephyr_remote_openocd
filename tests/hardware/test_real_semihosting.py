# SPDX-License-Identifier: Apache-2.0

import pytest

from tests.acceptance.semihosting import SemihostingAcceptance
from tests.hardware_support import SemihostingFixture

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealSemihosting:
    def test_direct_semihosting_console_normal_completion(
        self, semihosting_fixture: SemihostingFixture
    ) -> None:
        SemihostingAcceptance().run(semihosting_fixture)
