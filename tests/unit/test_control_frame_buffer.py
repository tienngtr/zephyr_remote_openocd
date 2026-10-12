# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json

import pytest
from zephyr_remote_openocd.protocol import wire as protocol_wire


def test_control_frames_retain_partial_bytes_without_waiting_for_lf():
    frames = protocol_wire.ControlFrameBuffer()
    frames.feed(b'{"value":"\xc3')
    assert frames.pop_frame() is None
    frames.feed(b'\xa9"}')
    assert frames.pop_frame() is None
    frames.feed(b"\n")
    frame = frames.pop_frame()
    assert frame is not None
    assert json.loads(frame) == {"value": "é"}
    frames.finish()


@pytest.mark.parametrize("payload", (b"x" * 32, b"x" * 32 + b"\n"))
def test_control_frames_reject_oversized_input(monkeypatch, payload):
    monkeypatch.setattr(protocol_wire, 'MAX_FRAME_SIZE', 32)
    frames = protocol_wire.ControlFrameBuffer()
    frames.feed(payload)
    with pytest.raises(ValueError):
        frames.pop_frame()
