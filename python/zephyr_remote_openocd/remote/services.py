"""Helper-independent loopback allocation primitives."""

from __future__ import annotations

import ipaddress
import secrets
from typing import Callable, TypeVar

LOOPBACK_RANGE = ipaddress.IPv4Network("127.64.0.0/10")
T = TypeVar("T")


def random_loopback_address() -> str:
    offset = 1 + secrets.randbelow(LOOPBACK_RANGE.num_addresses - 2)
    return str(ipaddress.IPv4Address(int(LOOPBACK_RANGE.network_address) + offset))


def allocate_loopback(attempt: Callable[[str], T], *, attempts: int = 32) -> tuple[str, T]:
    """Try an address-dependent operation, treating OSError as a collision."""
    last_error: OSError | None = None
    for _ in range(attempts):
        address = random_loopback_address()
        try:
            return address, attempt(address)
        except OSError as error:
            last_error = error
    raise RuntimeError(f"loopback allocation exhausted after {attempts} attempts") from last_error
