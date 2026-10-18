# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import select
import signal
import socket
import subprocess
import sys
from unittest.mock import create_autospec

import pytest
from zephyr_remote_openocd.helper import _addresses as helper_addresses
from zephyr_remote_openocd.helper import _workspace as workspace_module

from tests.support import ROOT


@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_address_probe_finalizer_failure_rolls_back_every_socket(monkeypatch, failure_type):
    original_socket = socket.socket
    sockets = []
    failure = failure_type("address probe close interrupted adoption")

    def acquire(*args, **kwargs):
        owned = original_socket(*args, **kwargs)
        sockets.append(owned)
        controlled = create_autospec(original_socket, instance=True, spec_set=True)
        controlled.bind.side_effect = owned.bind
        if len(sockets) == 2:

            def close_then_fail():
                owned.close()
                raise failure

            controlled.close.side_effect = close_then_fail
        else:
            controlled.close.side_effect = owned.close
        return controlled

    monkeypatch.setattr(helper_addresses.socket, "socket", acquire)
    try:
        with pytest.raises(failure_type) as raised:
            helper_addresses.allocate_listener_address((0, 0))
        assert raised.value is failure
        assert len(sockets) == 3
        assert all(owned.fileno() == -1 for owned in sockets)
    finally:
        for owned in sockets:
            owned.close()


def test_preferred_address_is_leased_before_random_allocation(
    monkeypatch, isolated_lease_addresses
):
    preferred, _ = isolated_lease_addresses

    def unexpected_random():
        raise AssertionError("a free preferred address should be reused")

    monkeypatch.setattr(helper_addresses, 'random_address', unexpected_random)
    allocated = helper_addresses.allocate_listener_address((), preferred_address=preferred)
    try:
        assert allocated == preferred
    finally:
        allocated.lease.close()


def test_preferred_address_held_by_another_session_falls_back(
    monkeypatch, isolated_lease_addresses
):
    preferred, fallback = isolated_lease_addresses
    first = helper_addresses.allocate_listener_address((), preferred_address=preferred)
    monkeypatch.setattr(helper_addresses, 'random_address', lambda: fallback)
    try:
        second = helper_addresses.allocate_listener_address((), preferred_address=preferred)
        try:
            assert second == fallback
        finally:
            second.lease.close()
    finally:
        first.lease.close()


def test_preferred_address_with_occupied_service_port_falls_back(
    monkeypatch, isolated_lease_addresses
):
    preferred, fallback = isolated_lease_addresses
    monkeypatch.setattr(helper_addresses, 'random_address', lambda: fallback)
    with socket.socket() as listener:
        listener.bind((preferred, 0))
        port = listener.getsockname()[1]
        allocated = helper_addresses.allocate_listener_address((port,), preferred_address=preferred)
        try:
            assert allocated == fallback
            # Rejected candidates must release their address lease.
            reused = helper_addresses.allocate_listener_address((), preferred_address=preferred)
            reused.lease.close()
        finally:
            allocated.lease.close()


@pytest.mark.parametrize("termination", ("exit", "kill"))
def test_address_lease_coordinates_helpers_and_releases_on_exit(
    tmp_path, monkeypatch, isolated_lease_addresses, termination
):
    address, other_address = isolated_lease_addresses
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path / "local-user")
    candidates = iter((address, other_address, address))
    monkeypatch.setattr(helper_addresses, 'random_address', lambda: next(candidates))
    with subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tests/fixtures/address_lease_holder.py"),
            str(tmp_path / "other-user"),
            address,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            assert select.select([process.stdout], [], [], 30)[0]
            assert process.stdout.readline() == f"{address}\n".encode()
            allocated = helper_addresses.allocate_listener_address(())
            try:
                assert allocated == other_address
            finally:
                allocated.lease.close()
            if termination == "kill":
                process.kill()
            else:
                process.stdin.close()
            expected_status = -signal.SIGKILL if termination == "kill" else 0
            assert process.wait(timeout=30) == expected_status
            reused = helper_addresses.allocate_listener_address(())
            try:
                assert reused == address
            finally:
                reused.lease.close()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)
