# SPDX-License-Identifier: Apache-2.0

"""Incremental child output decoding and readiness-marker observation."""

from __future__ import annotations

import codecs
from collections.abc import Iterable
from typing import NamedTuple

from zephyr_remote_openocd.helper import _streams as streams


class RequiredOutputSentinels:
    """Session-owned state of required complete output lines."""

    def __init__(self, required_output_sentinels: Iterable[str]) -> None:
        self._unseen = set(required_output_sentinels)

    @property
    def ready(self) -> bool:
        return not self._unseen

    @property
    def unseen(self) -> tuple[str, ...]:
        return tuple(self._unseen)

    def observe(self, sentinel: str) -> None:
        self._unseen.discard(sentinel)


class _SentinelMatcher:
    """Recognize required startup output markers as complete trimmed lines."""

    def __init__(self, required_output_sentinels: RequiredOutputSentinels) -> None:
        self.required_output_sentinels = required_output_sentinels
        self._reset_line()

    def _reset_line(self) -> None:
        self._candidates = {sentinel: 0 for sentinel in self.required_output_sentinels.unseen}
        self._started = False

    def feed(self, character: str) -> None:
        if self.required_output_sentinels.ready or not self._candidates:
            return
        if not self._started and character.isspace():
            return
        self._started = True
        for sentinel, index in tuple(self._candidates.items()):
            if index < len(sentinel) and character == sentinel[index]:
                self._candidates[sentinel] = index + 1
            elif index == len(sentinel) and character.isspace():
                continue
            else:
                del self._candidates[sentinel]

    def finish_line(self) -> None:
        for sentinel, index in self._candidates.items():
            if self._started and index == len(sentinel):
                self.required_output_sentinels.observe(sentinel)
        self._reset_line()


class CapturedFragment(NamedTuple):
    """Retained startup output with its stream and boundary metadata."""

    stream: str
    payload: str
    line_end: bool


class OutputDecoder:
    """Decode one observed stream, preserving fragments and sentinel lines."""

    def __init__(
        self, stream_name: str, required_output_sentinels: RequiredOutputSentinels
    ) -> None:
        self.stream_name = stream_name
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.matcher = _SentinelMatcher(required_output_sentinels)
        self.pending: list[str] = []

    def feed(self, chunk: bytes) -> list[CapturedFragment]:
        fragments: list[CapturedFragment] = []

        def fragment(line_end: bool = False) -> None:
            fragments.append(CapturedFragment(self.stream_name, "".join(self.pending), line_end))
            self.pending.clear()

        for character in self.decoder.decode(chunk, final=not chunk):
            if character == "\n":
                self.matcher.finish_line()
                fragment(line_end=True)
                continue
            self.matcher.feed(character)
            self.pending.append(character)
            if len(self.pending) >= streams.RELAY_CHUNK_SIZE:
                fragment()
        if not chunk:
            self.matcher.finish_line()
        if self.pending:
            fragment()
        return fragments
