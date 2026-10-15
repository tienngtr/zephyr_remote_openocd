# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress

import pytest
from zephyr_remote_openocd.helper import _streams as helper_streams


@pytest.mark.parametrize("boundary", ("reader", "writer"))
@pytest.mark.parametrize("rollback_fails", (False, True))
def test_nonblocking_acquisition_rolls_back_effect_before_failed_adoption(
    monkeypatch, boundary, rollback_fails
):
    read_fd, write_fd = os.pipe()
    original_set_blocking = os.set_blocking
    primary = KeyboardInterrupt("descriptor adoption interrupted")
    secondary = OSError("descriptor rollback failed")
    secondary.add_note("rollback retained nested detail")

    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "w", encoding="utf-8") as stdout:
        descriptor = read_fd if boundary == "reader" else write_fd

        def set_blocking(target, blocking):
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


def test_protocol_writer_close_releases_pending_buffer_after_interruption(monkeypatch):
    read_fd, write_fd = os.pipe()
    original_set_blocking = os.set_blocking
    failure = KeyboardInterrupt("writer mode restoration interrupted")
    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "w", encoding="utf-8") as stdout:
        monkeypatch.setattr(sys, "stdout", stdout)
        output = helper_streams.ProtocolOutput()
        output.enqueue(b"queued frame\n")

        def set_blocking(descriptor, blocking):
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


@pytest.mark.parametrize("source", ("available-prefix", "continuous"))
def test_final_reader_scan_consumes_available_prefix_with_finite_budget(monkeypatch, source):
    read_fd, write_fd = os.pipe()
    original_read = os.read
    prefix = b"prefix before READY"

    def read(descriptor, size):
        if descriptor != read_fd:
            return original_read(descriptor, size)
        if source == "continuous":
            return b"x" * size
        return original_read(descriptor, min(size, 3))

    monkeypatch.setattr(os, "read", read)

    async def run():
        reader = helper_streams.AsyncInput(read_fd)
        next_read = None
        try:
            os.write(write_fd, prefix)
            checkpoint = reader.checkpoint()
            captured = bytearray(await reader.read())
            assert not checkpoint.done()
            if source == "continuous":
                while not checkpoint.done():
                    captured.extend(await reader.read())
                assert len(captured) == helper_streams.MAX_FINAL_OBSERVATION_BYTES
            else:
                while len(captured) < len(prefix):
                    captured.extend(await reader.read())
                assert bytes(captured) == prefix
                next_read = asyncio.create_task(reader.read())
                await checkpoint
                assert not next_read.done()
        finally:
            if next_read is not None:
                next_read.cancel()
                with suppress(asyncio.CancelledError):
                    await next_read
            reader.close()

    try:
        asyncio.run(run())
        assert os.get_blocking(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)
