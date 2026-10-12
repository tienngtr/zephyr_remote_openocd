# SPDX-License-Identifier: Apache-2.0

"""Canonical Protocol v1 preferred-address domain."""

from __future__ import annotations

import ipaddress

LOOPBACK_RANGE = ipaddress.IPv4Network("127.64.0.0/10")


def validate_preferred_address(address: str | None) -> None:
    """Validate a canonical, usable address hint without reserving it."""
    if address is None:
        return
    if not isinstance(address, str):
        raise ValueError("preferred address must be a string or null")
    parsed = ipaddress.IPv4Address(address)
    if (
        str(parsed) != address
        or parsed not in LOOPBACK_RANGE
        or parsed in (LOOPBACK_RANGE.network_address, LOOPBACK_RANGE.broadcast_address)
    ):
        raise ValueError("preferred address must be a usable address in 127.64.0.0/10")
