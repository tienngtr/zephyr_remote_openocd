# SPDX-License-Identifier: Apache-2.0

import os
from queue import Queue
from unittest.mock import patch

import pytest

from tests.process_support import ProcessOutputMonitor


def test_process_output_monitor_signals_pattern_and_captures_output() -> None:
    requests: Queue[None] = Queue()
    chunks: Queue[bytes] = Queue()

    def controlled_read(_file_descriptor: int, _size: int) -> bytes:
        requests.put(None)
        return chunks.get(timeout=5)

    read_fd, write_fd = os.pipe()
    try:
        with (
            os.fdopen(read_fd, "rb") as stream,
            patch("tests.process_support.os.read", side_effect=controlled_read),
        ):
            monitor = ProcessOutputMonitor(stream)
            requests.get(timeout=5)
            chunks.put(b"starting\nGDB_READY\n")
            monitor.wait_for("GDB_READY", timeout=5)
            requests.get(timeout=5)
            chunks.put(b"remaining\n")
            requests.get(timeout=5)
            chunks.put(b"")
            monitor.join(timeout=5)
            assert stream.closed
    finally:
        os.close(write_fd)

    assert monitor.text == "starting\nGDB_READY\nremaining\n"


def test_process_output_monitor_join_reports_late_read_failure() -> None:
    requests: Queue[None] = Queue()
    responses: Queue[bytes | OSError] = Queue()

    def controlled_read(_file_descriptor: int, _size: int) -> bytes:
        requests.put(None)
        response = responses.get(timeout=5)
        if isinstance(response, OSError):
            raise response
        return response

    read_fd, write_fd = os.pipe()
    try:
        with (
            os.fdopen(read_fd, "rb") as stream,
            patch("tests.process_support.os.read", side_effect=controlled_read),
        ):
            monitor = ProcessOutputMonitor(stream)
            requests.get(timeout=5)
            responses.put(b"GDB_READY\n")
            monitor.wait_for("GDB_READY", timeout=5)
            requests.get(timeout=5)
            responses.put(OSError("reader failed"))

            with pytest.raises(AssertionError, match="process output read failed: reader failed"):
                monitor.join(timeout=5)
            assert stream.closed
    finally:
        os.close(write_fd)
