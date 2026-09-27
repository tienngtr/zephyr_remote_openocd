# SPDX-License-Identifier: Apache-2.0

"""Deadline-aware reads for test-owned pipes (do not mix with buffered reads)."""

from __future__ import annotations

import os
import re
import selectors
import threading
import time
from typing import BinaryIO


class ProcessOutputMonitor:
    """Continuously capture a process pipe and signal observable output."""

    def __init__(self, stream: BinaryIO):
        self._stream = stream
        self._fd = stream.fileno()
        self._output = bytearray()
        self._condition = threading.Condition()
        self._finished = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            while chunk := os.read(self._fd, 4096):
                with self._condition:
                    self._output.extend(chunk)
                    self._condition.notify_all()
        except BaseException as error:
            with self._condition:
                self._error = error
        finally:
            with self._condition:
                self._finished = True
                self._condition.notify_all()

    @property
    def text(self) -> str:
        with self._condition:
            return bytes(self._output).decode("utf-8", "replace")

    def wait_for(self, pattern: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        encoded = pattern.encode()
        with self._condition:
            while encoded not in self._output:
                if self._error is not None:
                    raise AssertionError(
                        f"process output read failed: {self._error}"
                    ) from self._error
                if self._finished:
                    raise AssertionError(
                        f"process output ended before {pattern!r} was observed:\n{self.text}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"pattern {pattern!r} not observed before timeout:\n{self.text}"
                    )
                self._condition.wait(remaining)

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise AssertionError("process output reader did not terminate")
        try:
            if self._error is not None:
                raise AssertionError(f"process output read failed: {self._error}") from self._error
        finally:
            self._stream.close()


def assert_semihosting_acceptance(returncode: int | None, output: str, pattern: str) -> None:
    """Require natural command success and the configured semihosting output."""
    assert returncode == 0, output
    assert re.search(pattern, output), output


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
