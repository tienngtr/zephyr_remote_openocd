# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import shlex

from tests.serial_reader import remote_serial_reader_command


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
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "-c"]
    assert argv[3:] == ["/dev/tty example", "921600", "7", "even", "2", "hardware", "ready", "3.5"]
