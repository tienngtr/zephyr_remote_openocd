# SPDX-License-Identifier: Apache-2.0

import os

import tests.process_support as process_support


def test_process_output_monitor_signals_pattern_and_captures_output() -> None:
    assert hasattr(process_support, "ProcessOutputMonitor")
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as stream:
        monitor = process_support.ProcessOutputMonitor(stream)
        try:
            os.write(write_fd, b"starting\nGDB_READY\n")
            monitor.wait_for("GDB_READY", timeout=1)
            os.write(write_fd, b"remaining\n")
        finally:
            os.close(write_fd)
        monitor.join(timeout=1)

    assert monitor.text == "starting\nGDB_READY\nremaining\n"
