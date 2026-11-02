# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import sys

import pytest
from zephyr_remote_openocd.helper import _streams as helper_streams


@pytest.mark.parametrize("boundary", ("reader", "writer"))
@pytest.mark.parametrize("rollback_fails", (False, True))
def test_nonblocking_acquisition_rolls_back_effect_before_failed_adoption(
    monkeypatch: pytest.MonkeyPatch, boundary: str, rollback_fails: bool
) -> None:
    read_fd, write_fd = os.pipe()
    original_set_blocking = os.set_blocking
    primary = KeyboardInterrupt("descriptor adoption interrupted")
    secondary = OSError("descriptor rollback failed")
    secondary.add_note("rollback retained nested detail")

    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "w", encoding="utf-8") as stdout:
        descriptor = read_fd if boundary == "reader" else write_fd

        def set_blocking(target: int, blocking: bool) -> None:
            original_set_blocking(target, blocking)
            if target == descriptor:
                if not blocking:
                    raise primary
                if rollback_fails:
                    raise secondary

        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(os, "set_blocking", set_blocking)
        with pytest.raises(KeyboardInterrupt) as raised:
            if boundary == "reader":
                helper_streams.AsyncInput(descriptor)
            else:
                helper_streams.ProtocolOutput()
        assert raised.value is primary
        assert os.get_blocking(descriptor)
        if rollback_fails:
            assert any(str(secondary) in note for note in primary.__notes__)
            assert any(secondary.__notes__[0] in note for note in primary.__notes__)


def test_protocol_writer_close_releases_pending_buffer_after_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    original_set_blocking = os.set_blocking
    failure = KeyboardInterrupt("writer mode restoration interrupted")
    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "w", encoding="utf-8") as stdout:
        monkeypatch.setattr(sys, "stdout", stdout)
        output = helper_streams.ProtocolOutput()
        output.enqueue(b"queued frame\n")

        def set_blocking(descriptor: int, blocking: bool) -> None:
            original_set_blocking(descriptor, blocking)
            if descriptor == write_fd and blocking:
                raise failure

        monkeypatch.setattr(os, "set_blocking", set_blocking)
        with pytest.raises(KeyboardInterrupt) as raised:
            output.close()
        assert raised.value is failure
        assert not output.frames
        assert output.pending_bytes == 0
        assert os.get_blocking(write_fd)
