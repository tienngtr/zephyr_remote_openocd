# SPDX-License-Identifier: Apache-2.0

"""Exercise test oracles with real pipes; no SSH or target hardware."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager

import pytest

from tests.process_support import (
    assert_semihosting_acceptance,
    read_line,
    read_lines,
    read_until,
)


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
    ("returncode", "output", "error"),
    (
        (0, "console", None),
        (1, "console", AssertionError),
        (0, "wrong output", AssertionError),
        (-9, "console", AssertionError),
    ),
)
def test_semihosting_acceptance_requires_output_and_natural_success(returncode, output, error):
    if error:
        with pytest.raises(error):
            assert_semihosting_acceptance(returncode, output, "console")
    else:
        assert_semihosting_acceptance(returncode, output, "console")
