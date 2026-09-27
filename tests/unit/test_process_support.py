# SPDX-License-Identifier: Apache-2.0

from queue import Queue
from typing import BinaryIO, cast
from unittest.mock import patch

import tests.process_support as process_support


class DescriptorStream:
    def fileno(self) -> int:
        return 17


def test_process_output_monitor_signals_pattern_and_captures_output() -> None:
    assert hasattr(process_support, "ProcessOutputMonitor")
    requests: Queue[None] = Queue()
    chunks: Queue[bytes] = Queue()

    def controlled_read(file_descriptor: int, size: int) -> bytes:
        assert file_descriptor == 17
        assert size == 4096
        requests.put(None)
        return chunks.get(timeout=5)

    with patch("tests.process_support.os.read", side_effect=controlled_read):
        monitor = process_support.ProcessOutputMonitor(cast(BinaryIO, DescriptorStream()))
        requests.get(timeout=5)
        chunks.put(b"starting\nGDB_READY\n")
        monitor.wait_for("GDB_READY", timeout=5)
        requests.get(timeout=5)
        chunks.put(b"remaining\n")
        requests.get(timeout=5)
        chunks.put(b"")
        monitor.join(timeout=5)

    assert monitor.text == "starting\nGDB_READY\nremaining\n"
