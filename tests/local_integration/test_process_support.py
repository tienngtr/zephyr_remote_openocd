# SPDX-License-Identifier: Apache-2.0

"""Exercise test oracles with real pipes; no SSH or target hardware."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tests.hardware import test_real_semihosting as semihosting
from tests.process_support import read_line, read_lines, read_until


@contextmanager
def pipe(payload=b""):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as reader:
        try:
            os.write(write_fd, payload)
            yield reader
        finally:
            os.close(write_fd)


@pytest.mark.parametrize("payload", (b"", b"partial"))
def test_line_deadline_includes_silence_and_partial_lines(payload):
    with pipe(payload) as reader, pytest.raises(AssertionError, match="timed out"):
        read_line(reader, timeout=0.02)


def test_line_reader_does_not_lose_coalesced_lines():
    with pipe(b"first\nsecond\n") as reader:
        assert read_line(reader) == b"first\n"
        assert read_line(reader) == b"second\n"


def test_read_through_eof_has_deadline_even_after_complete_lines():
    with pipe(b"event\n") as reader, pytest.raises(AssertionError, match="timed out"):
        list(read_lines(reader, timeout=0.02))


def test_rtt_reader_matches_markers_in_same_chunk_after_exit():
    with subprocess.Popen(
        [sys.executable, "-c", "print('endpoint ready', flush=True)"],
        stdout=subprocess.PIPE,
    ) as process:
        process.wait(timeout=5)
        output = bytearray()
        read_until(process, "endpoint", 1, output)
        read_until(process, "ready", 1, output)
        with pytest.raises(AssertionError, match="missing"):
            read_until(process, "missing", 1, output)


@pytest.mark.parametrize(
    ("code", "error"),
    (
        ("print('console'); raise SystemExit(0)", None),
        ("print('console'); raise SystemExit(1)", AssertionError),
        ("print('wrong output')", AssertionError),
        ("print('console', flush=True); import time; time.sleep(60)", subprocess.TimeoutExpired),
    ),
)
def test_semihosting_acceptance_requires_output_and_natural_success(code, error):
    oracle = semihosting.TestRealSemihosting()
    fixture = {"semihosting_gdb_init": [], "expected_output": "console", "timeout": 0.5}
    with (
        patch.object(oracle, "_flash"),
        patch.object(oracle, "_west", return_value=[sys.executable, "-c", code]),
        patch.object(oracle, "_environment", return_value=os.environ.copy()),
        patch.object(oracle, "_assert_cleanup") as cleanup,
    ):
        if error:
            with pytest.raises(error):
                oracle.test_direct_semihosting_console_normal_completion(fixture)
            cleanup.assert_not_called()
        else:
            oracle.test_direct_semihosting_console_normal_completion(fixture)
            cleanup.assert_called_once()
