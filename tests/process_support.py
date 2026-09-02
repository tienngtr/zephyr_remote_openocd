# SPDX-License-Identifier: Apache-2.0

"""Deadline-aware reads for test-owned pipes (do not mix with buffered reads)."""

from __future__ import annotations

import os
import re
import selectors
import time


def read_line(stream, timeout=30):
    """Read one binary line without prefetching bytes needed by communicate()."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise AssertionError(f"output line timed out; partial output: {bytes(data)!r}")
            chunk = os.read(stream.fileno(), 1)
            data.extend(chunk)
            if not chunk or chunk == b"\n":
                return bytes(data)


def read_lines(stream, timeout=30):
    """Read through EOF under one deadline, including continuously chatty children."""
    deadline = time.monotonic() + timeout
    while line := read_line(stream, deadline - time.monotonic()):
        yield line


def read_until(process, pattern, timeout, output):
    """Match accumulated output before waiting for additional pipe data."""
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while not re.search(pattern.encode(), output):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                break
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            output.extend(chunk)
        else:
            return
    raise AssertionError(
        f"pattern {pattern!r} not observed; status={process.poll()}:\n"
        + bytes(output).decode("utf-8", "replace")
    )
