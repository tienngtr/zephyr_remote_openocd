# SPDX-License-Identifier: Apache-2.0

"""Exercise test oracles with real pipes; no SSH or target hardware."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from io import FileIO
from types import TracebackType
from typing import IO, Literal, Self

import pytest

from tests import process_support
from tests.process_support import (
    read_line,
    read_lines,
    read_until,
)


@contextmanager
def pipe(payload: bytes = b"") -> Iterator[FileIO]:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as reader:
        try:
            os.write(write_fd, payload)
            yield reader
        finally:
            os.close(write_fd)


def control_deadline(monkeypatch: pytest.MonkeyPatch, readable_bytes: int) -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    class Selector:
        def __enter__(self) -> Self:
            return self

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc_value: BaseException | None,
            _traceback: TracebackType | None,
        ) -> Literal[False]:
            return False

        @staticmethod
        def register(_stream: IO[bytes], _events: int) -> None:
            pass

        def select(self, timeout: float) -> list[tuple[None, None]]:
            if state["readable_bytes"]:
                state["readable_bytes"] -= 1
                return [(None, None)]
            clock.now += timeout
            return []

    clock = Clock()
    state = {"readable_bytes": readable_bytes}
    monkeypatch.setattr(process_support.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(process_support.selectors, "DefaultSelector", Selector)


@pytest.mark.parametrize("payload", (b"", b"partial"))
def test_line_deadline_includes_silence_and_partial_lines(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    control_deadline(monkeypatch, len(payload))
    with pipe(payload) as reader, pytest.raises(AssertionError) as raised:
        read_line(reader)

    if payload:
        assert repr(payload) in str(raised.value)


def test_line_reader_does_not_lose_coalesced_lines() -> None:
    with pipe(b"first\nsecond\n") as reader:
        assert read_line(reader) == b"first\n"
        assert read_line(reader) == b"second\n"


def test_read_through_eof_has_deadline_even_after_complete_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"event\n"
    control_deadline(monkeypatch, len(payload))
    with pipe(payload) as reader, pytest.raises(AssertionError):
        list(read_lines(reader))


def test_rtt_reader_matches_markers_in_same_chunk_after_exit() -> None:
    with subprocess.Popen(
        [sys.executable, "-c", "print('endpoint ready', flush=True)"],
        stdout=subprocess.PIPE,
    ) as process:
        process.wait(timeout=5)
        output = bytearray()
        read_until(process, "endpoint", timeout=1, output=output)
        read_until(process, "ready", timeout=1, output=output)
        with pytest.raises(AssertionError):
            read_until(process, "missing", timeout=1, output=output)
