# SPDX-License-Identifier: Apache-2.0

"""Physical remote loopback address leases and listener bind probes."""

from __future__ import annotations

import errno
import ipaddress
import secrets
import socket
from collections.abc import Sequence
from itertools import chain
from typing import Self

from zephyr_remote_openocd.helper._cleanup import raise_cleanup_errors
from zephyr_remote_openocd.protocol.addresses import LOOPBACK_RANGE

MAX_ADDRESS_ALLOCATION_ATTEMPTS = 32


def random_address() -> str:
    # Exclude network/broadcast endpoints without material bias.
    return str(
        ipaddress.IPv4Address(
            int(LOOPBACK_RANGE.network_address)
            + 1
            + secrets.randbelow(LOOPBACK_RANGE.num_addresses - 2)
        )
    )


class AllocatedAddress(str):
    """An allocated address with a lease held for the session lifetime."""

    lease: socket.socket

    def __new__(cls, address: str, lease: socket.socket) -> Self:
        value = super().__new__(cls, address)
        value.lease = lease
        return value


def allocate_listener_address(
    ports: Sequence[int], preferred_address: str | None = None
) -> AllocatedAddress:
    preferred = () if preferred_address is None else (preferred_address,)
    candidates = chain(
        preferred, (random_address() for _ in range(MAX_ADDRESS_ALLOCATION_ATTEMPTS))
    )
    for address in candidates:
        # Abstract sockets share the TCP network namespace across remote users
        # and runtime roots. Keep the name independent of helper revisions.
        lease = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            lease.bind(f"\0zephyr_remote_openocd.address.{address}")
        except BaseException as exc:
            try:
                lease.close()
            except BaseException as cleanup_error:
                raise_cleanup_errors([exc, cleanup_error])
            if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
                continue
            raise
        sockets = []
        primary = None
        try:
            for port in ports:
                candidate = socket.socket()
                sockets.append(candidate)
                candidate.bind((address, port))
        except BaseException as exc:
            primary = exc
        cleanup_errors = []
        for candidate in sockets:
            try:
                candidate.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if primary is None and not cleanup_errors:
            try:
                # Transfer only after independent probes settle; until the
                # return value is constructed, the allocator owns rollback.
                return AllocatedAddress(address, lease)
            except BaseException as exc:
                primary = exc
        try:
            lease.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if isinstance(primary, OSError) and not cleanup_errors:
            continue
        if primary is not None:
            cleanup_errors.insert(0, primary)
        raise_cleanup_errors(cleanup_errors)
    raise RuntimeError(
        f"loopback allocation exhausted after {MAX_ADDRESS_ALLOCATION_ATTEMPTS} attempts"
    )
