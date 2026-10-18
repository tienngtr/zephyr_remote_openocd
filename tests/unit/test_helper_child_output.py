# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from zephyr_remote_openocd.helper import _streams as helper_streams
from zephyr_remote_openocd.helper._child_output import (
    CapturedFragment,
    OutputDecoder,
    RequiredOutputSentinels,
)


def test_decoder_emits_short_fragment_before_complete_sentinel() -> None:
    sentinels = RequiredOutputSentinels(("READY",))
    decoder = OutputDecoder("stdout", sentinels)
    assert decoder.feed(b"READY") == [CapturedFragment("stdout", "READY", False)]
    assert not sentinels.ready
    assert decoder.feed(b"\n") == [CapturedFragment("stdout", "", True)]
    assert sentinels.ready


@pytest.mark.parametrize(
    ("first", "second"),
    (("OPENOCD INIT", "STARTUP COMPLETE"), ("STARTUP COMPLETE", "OPENOCD INIT")),
    ids=("init-first", "startup-first"),
)
def test_decoder_waits_for_each_complete_sentinel_across_streams(first: str, second: str) -> None:
    sentinels = RequiredOutputSentinels(("OPENOCD INIT", "STARTUP COMPLETE"))
    stdout = OutputDecoder("stdout", sentinels)
    stderr = OutputDecoder("stderr", sentinels)
    for chunk in (b"diagnostic\n", first[:8].encode(), (first[8:] + "\n").encode()):
        stdout.feed(chunk)
    assert not sentinels.ready
    stderr.feed(("  " + second + "  \n").encode())
    assert sentinels.ready


@pytest.mark.parametrize("payload", (b"abcdefghij\n", b"tail", b"\n\nx\n", b""))
def test_decoder_metadata_reconstructs_logical_output(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr(helper_streams, "RELAY_CHUNK_SIZE", 4)
    decoder = OutputDecoder("stdout", RequiredOutputSentinels(()))
    fragments = decoder.feed(payload) + decoder.feed(b"")
    reconstructed = "".join(item.payload + ("\n" if item.line_end else "") for item in fragments)
    assert reconstructed == payload.decode()
    assert all(item.stream == "stdout" for item in fragments)
    assert all(len(item.payload) <= 4 for item in fragments)
    assert all(item.payload or item.line_end for item in fragments)
    assert sum(item.line_end for item in fragments) == payload.count(b"\n")


def test_decoder_preserves_split_utf8_and_invalid_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper_streams, "RELAY_CHUNK_SIZE", 3)
    decoder = OutputDecoder("stderr", RequiredOutputSentinels(()))
    fragments = []
    for chunk in (b"utf \xe2", b"\x82", b"\xac\ninvalid \xff", b""):
        fragments.extend(decoder.feed(chunk))
    assert "".join(item.payload + ("\n" if item.line_end else "") for item in fragments) == (
        "utf €\ninvalid �"
    )
    assert all(item.stream == "stderr" and len(item.payload) <= 3 for item in fragments)
