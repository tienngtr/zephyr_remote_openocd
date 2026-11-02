# SPDX-License-Identifier: Apache-2.0

"""Incremental child output decoding."""

from __future__ import annotations

import codecs
from typing import NamedTuple

from zephyr_remote_openocd.helper import _streams as streams


class CapturedFragment(NamedTuple):
    """Retained startup output with its stream and boundary metadata."""

    stream: str
    payload: str
    line_end: bool


class OutputDecoder:
    """Decode one observed stream, preserving fragments and line boundaries."""

    def __init__(self, stream_name: str) -> None:
        self.stream_name = stream_name
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pending: list[str] = []

    def feed(self, chunk: bytes) -> list[CapturedFragment]:
        fragments: list[CapturedFragment] = []

        def fragment(line_end: bool = False) -> None:
            fragments.append(CapturedFragment(self.stream_name, "".join(self.pending), line_end))
            self.pending.clear()

        for character in self.decoder.decode(chunk, final=not chunk):
            if character == "\n":
                fragment(line_end=True)
                continue
            self.pending.append(character)
            if len(self.pending) >= streams.RELAY_CHUNK_SIZE:
                fragment()
        if self.pending:
            fragment()
        return fragments
