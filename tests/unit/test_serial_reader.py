# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from tests.serial_reader import SERIAL_READER_SOURCE, remote_serial_reader_command


def test_reader_command_carries_structured_framing_and_expectation():
    command = remote_serial_reader_command(
        "/dev/tty example",
        921600,
        "ready",
        3.5,
        data_bits=7,
        parity="even",
        stop_bits=2,
        flow_control="hardware",
    )
    assert "python3 -c" in command
    assert "'/dev/tty example'" in command
    assert "921600 7 even 2 hardware ready 3.5" in command
    assert "termios.tcflush" in SERIAL_READER_SOURCE
    assert "READY" in SERIAL_READER_SOURCE
    assert "MATCH" in SERIAL_READER_SOURCE


def test_reader_source_is_not_dependent_on_pyserial_or_external_terminal():
    assert "serial" not in SERIAL_READER_SOURCE.lower()
    assert "termios" in SERIAL_READER_SOURCE
    assert "select" in SERIAL_READER_SOURCE
