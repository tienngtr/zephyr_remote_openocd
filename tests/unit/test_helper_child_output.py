# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
from zephyr_remote_openocd.helper import _child_output as helper_child_output
from zephyr_remote_openocd.helper import _streams as helper_streams


def _decode_chunks(chunks: tuple[bytes, ...], name: str, sentinels=None) -> None:
    if sentinels is None:
        sentinels = helper_child_output.RequiredOutputSentinels(())
    decoder = helper_child_output.OutputDecoder(name, sentinels)
    for chunk in (*chunks, b""):
        for fragment in decoder.feed(chunk):
            helper_streams.emit(
                "CHILD_OUTPUT",
                stream=fragment.stream,
                payload=fragment.payload,
                line_end=fragment.line_end,
            )


def test_relay_emits_short_fragment_before_complete_sentinel():
    sentinels = helper_child_output.RequiredOutputSentinels(("READY",))
    decoder = helper_child_output.OutputDecoder("stdout", sentinels)
    fragments = decoder.feed(b"READY")
    assert [(item.payload, item.line_end) for item in fragments] == [("READY", False)]
    assert not sentinels.ready
    fragments = decoder.feed(b"\n")
    assert [(item.payload, item.line_end) for item in fragments] == [("", True)]
    assert sentinels.ready


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("OPENOCD INIT", "STARTUP COMPLETE"),
        ("STARTUP COMPLETE", "OPENOCD INIT"),
    ),
    ids=("init-first", "startup-first"),
)
def test_relay_waits_for_each_complete_sentinel_across_streams(monkeypatch, first, second):
    monkeypatch.setattr(helper_streams, 'RELAY_CHUNK_SIZE', 8)
    monkeypatch.setattr(helper_streams, 'emit', lambda *_args, **_kwargs: None)
    required_output_sentinels = helper_child_output.RequiredOutputSentinels(
        ("OPENOCD INIT", "STARTUP COMPLETE")
    )

    _decode_chunks(
        (b"diagnostic\n", first[:8].encode(), (first[8:] + "\n").encode()),
        "stdout",
        required_output_sentinels,
    )

    assert not required_output_sentinels.ready

    _decode_chunks(
        (("  " + second + "  \n").encode(),),
        "stderr",
        required_output_sentinels,
    )

    assert required_output_sentinels.ready


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (b"abcdefghij\n", "abcdefghij\n"),
        (b"tail", "tail"),
        (b"\n\nx\n", "\n\nx\n"),
        (b"", ""),
    ),
)
def test_relay_metadata_reconstructs_logical_output(monkeypatch, payload, expected):
    monkeypatch.setattr(helper_streams, 'RELAY_CHUNK_SIZE', 4)
    events = []
    monkeypatch.setattr(
        helper_streams,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )

    _decode_chunks((payload,), "stdout")

    output_events = [values for _kind, values in events]
    reconstructed = "".join(
        event["payload"] + ("\n" if event["line_end"] else "") for event in output_events
    )
    assert reconstructed == expected
    assert all(event["stream"] == "stdout" for event in output_events)
    assert all(len(event["payload"]) <= helper_streams.RELAY_CHUNK_SIZE for event in output_events)
    assert all(event["payload"] or event["line_end"] for event in output_events)
    assert sum(event["line_end"] for event in output_events) == payload.count(b"\n")


def test_relay_preserves_split_utf8_and_invalid_bytes(monkeypatch):
    monkeypatch.setattr(helper_streams, 'RELAY_CHUNK_SIZE', 3)
    events = []
    monkeypatch.setattr(
        helper_streams,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )

    _decode_chunks((b"utf \xe2", b"\x82", b"\xac\ninvalid \xff"), "stderr")

    output_events = [values for _kind, values in events]
    assert (
        "".join(event["payload"] + ("\n" if event["line_end"] else "") for event in output_events)
        == "utf €\ninvalid �"
    )
    assert all(len(event["payload"]) <= 3 for event in output_events)
