# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ipaddress

import pytest
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.remote.services import (
    LOOPBACK_RANGE,
    allocate_loopback,
    random_loopback_address,
)

TEST_PROCESS = ProcessSpec(("test-process",))


class TestAllocation:
    def test_range_and_exhaustion(self):
        assert ipaddress.IPv4Address(random_loopback_address()) in LOOPBACK_RANGE
        calls = []

        def collision(address):
            calls.append(address)
            raise OSError("occupied")

        with pytest.raises(RuntimeError):
            allocate_loopback(collision, attempts=3)
        assert len(calls) == 3
