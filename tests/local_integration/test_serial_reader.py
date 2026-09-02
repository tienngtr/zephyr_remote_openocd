# SPDX-License-Identifier: Apache-2.0

"""Run the actual remote-reader command against a local pseudo-terminal."""

from __future__ import annotations

import base64
import os
import shlex
import subprocess
import sys
import termios
from contextlib import contextmanager

import pytest

from tests.serial_reader import read_event, remote_serial_reader_command, stop_reader


@contextmanager
def reader_session(*, timeout=1, flow_control="none"):
    master, slave = os.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[2] |= termios.CRTSCTS
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        command = remote_serial_reader_command(
            os.ttyname(slave), 115200, "fresh marker", timeout, flow_control=flow_control
        )
        process = subprocess.Popen(
            [sys.executable, *shlex.split(command)[1:]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert read_event(process, 5)["type"] == "READY"
            yield process, master, slave
        finally:
            stop_reader(process)
    finally:
        os.close(master)
        os.close(slave)


def arm(process):
    process.stdin.write(b"ARM\n")
    process.stdin.flush()
    assert read_event(process, 5)["type"] == "ARMED"


def test_serial_reader_matches_fragmented_output_after_arm():
    with reader_session() as (process, master, _):
        arm(process)
        os.write(master, b"fresh ")
        os.write(master, b"marker\n")
        event = read_event(process, 5)
        assert event["type"] == "MATCH"
        assert b"fresh marker" in base64.b64decode(event["data"])
        assert process.wait(timeout=5) == 0


def test_serial_reader_discards_pre_arm_output_and_times_out():
    with reader_session(timeout=0.1) as (process, master, _):
        os.write(master, b"fresh marker\n")
        arm(process)
        event = read_event(process, 5)
        assert event["type"] == "TIMEOUT"
        assert b"fresh marker" not in base64.b64decode(event["data"])
        assert process.wait(timeout=5) == 2


def test_serial_reader_reports_invalid_arm():
    with reader_session() as (process, _, _):
        process.stdin.write(b"INVALID\n")
        process.stdin.flush()
        assert read_event(process, 5)["type"] == "ERROR"
        assert process.wait(timeout=5) == 3


@pytest.mark.parametrize("flow", ("none", "software", "hardware"))
def test_serial_reader_replaces_inherited_flow_control(flow):
    with reader_session(flow_control=flow) as (_, _, slave):
        attrs = termios.tcgetattr(slave)
        assert bool(attrs[2] & termios.CRTSCTS) is (flow == "hardware")
        assert bool(attrs[0] & termios.IXON) is (flow == "software")
        assert attrs[4:6] == [termios.B115200, termios.B115200]
