# SPDX-License-Identifier: Apache-2.0

"""Protect the hardware acceptance path at its process/socket boundaries."""

from __future__ import annotations

import os
import socket
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from tests.hardware.test_real_rtt import TestRealRtt as RttAcceptance
from tests.hardware_support import PreparedTarget, RttFixture
from tests.inventory import BuildEnvironment, InventoryHost, RttOperation, Toolchain


@pytest.mark.timeout(60)
def test_simultaneous_gdb_rtt_acceptance_does_not_require_runner_prose(tmp_path, monkeypatch):
    gdb_port, rtt_port = 12345, 12346
    target = PreparedTarget(
        "target:rtt",
        "target",
        "rtt",
        InventoryHost("remote", "unused", ("openocd",), ("ssh",), (), ()),
        BuildEnvironment("build", tmp_path, Path("west"), ()),
        Toolchain("toolchain", Path("gdb")),
        tmp_path,
        tmp_path / "config.yaml",
        (),
        (),
    )
    fixture = RttFixture(target, RttOperation(rtt_port, "pong", "ping", 30, True, "main"))

    with ExitStack() as resources:

        def output_pipe(payload):
            read_fd, write_fd = os.pipe()
            stream = resources.enter_context(os.fdopen(read_fd, "rb"))
            with os.fdopen(write_fd, "wb") as writer:
                writer.write(payload)
            return stream

        server = create_autospec(subprocess.Popen, instance=True)
        server.stdout = output_pipe(b"arbitrary noncontractual startup diagnostic\n")
        server.stdin = None
        server.stderr = None
        server.poll.return_value = None
        server.wait.return_value = 0
        client = create_autospec(subprocess.Popen, instance=True)
        client.stdout = output_pipe(b"ZRO_GDB_RTT_READY\n")
        client.stdin = None
        client.stderr = None
        client.poll.return_value = None
        client.communicate.return_value = (b"", None)
        client.returncode = 0
        processes = iter((server, client))
        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: next(processes))
        monkeypatch.setattr(
            "tests.hardware.test_real_rtt.free_loopback_ports", lambda count: [gdb_port]
        )
        gdb_connection, gdb_peer = socket.socketpair()
        rtt_connection, rtt_peer = socket.socketpair()
        for connection in (gdb_connection, gdb_peer, rtt_connection, rtt_peer):
            resources.enter_context(connection)
        rtt_peer.sendall(b"pong")
        rtt_peer.settimeout(30)

        def connect(address, *, timeout):
            assert address[0] == "127.0.0.1"
            if address[1] == gdb_port:
                return gdb_connection
            assert address[1] == rtt_port
            return rtt_connection

        monkeypatch.setattr(socket, "create_connection", connect)
        RttAcceptance().test_debugserver_serves_gdb_and_rtt_concurrently(fixture, tmp_path)

        assert gdb_connection.fileno() == -1
        assert rtt_peer.recv(4096) == b"ping"
        assert client.stdout.closed and server.stdout.closed
