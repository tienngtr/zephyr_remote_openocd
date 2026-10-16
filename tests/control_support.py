# SPDX-License-Identifier: Apache-2.0

"""Pipe-backed helper control transports for coordinator tests."""

from __future__ import annotations

import io
import os
import signal
import threading
from contextlib import suppress
from typing import BinaryIO

from zephyr_remote_openocd.protocol.wire import encode_frame

CREATED = encode_frame(
    "SESSION_CREATED", helper="fake", session_id="session", remote_workspace="/workspace"
)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


class ControlProcess:
    """Pipe-backed control transport whose event stream ends when it exits.

    Fault tests override the failing operation. The continuously owned helper
    reader consumes ``stdout`` from acquisition until the stream ends.
    """

    args = ("fake-helper",)
    # Pipe-only doubles have no kernel child. Their waits omit this completion
    # source; the live parent identity keeps descriptor acquisition harmless.
    pid = os.getpid()

    def __init__(self, initial: bytes = CREATED) -> None:
        self.stdin: BinaryIO = io.BytesIO()
        read_fd, self._events = os.pipe()
        self._events_lock = threading.Lock()
        _write_all(self._events, initial)
        self.stdout: BinaryIO = self._open_events(read_fd)
        self.stderr = io.BytesIO()
        self.returncode: int | None = None

    @staticmethod
    def _open_events(read_fd: int) -> BinaryIO:
        return os.fdopen(read_fd, "rb")

    def emit(self, frame: bytes) -> None:
        with self._events_lock:
            # An exited helper, or a reader whose stream is closed, drops events.
            if self._events >= 0:
                with suppress(BrokenPipeError):
                    _write_all(self._events, frame)

    def end_events(self) -> None:
        """End the event stream, as helper exit does; teardown releases its reader."""
        with self._events_lock:
            if self._events >= 0:
                os.close(self._events)
                self._events = -1

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        self.end_events()
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0
        self.end_events()

    def kill(self) -> None:
        self.returncode = -signal.SIGKILL
        self.end_events()

    @staticmethod
    def stderr_tail() -> bytes:
        return b""

    def close_stderr(self) -> None:
        self.stderr.close()


class HelperPeerProcess(ControlProcess):
    """A fake helper peer that emits its session events once START arrives.

    ``later`` events are withheld until the test releases them, so ordering
    against coordinator observations is explicit rather than scheduled.
    """

    def __init__(
        self,
        events: tuple[bytes, ...] = (),
        *,
        later: tuple[bytes, ...] | None = None,
        initial: bytes = CREATED,
    ) -> None:
        super().__init__(initial)
        read_fd, write_fd = os.pipe()
        self.stdin = os.fdopen(write_fd, "wb")
        self._written = bytearray()
        self._later = later
        self._responded = threading.Event()
        self._peer = threading.Thread(
            target=self._serve, args=(read_fd, b"".join(events)), daemon=True
        )
        self._peer.start()

    def _serve(self, read_fd: int, events: bytes) -> None:
        started = False
        with os.fdopen(read_fd, "rb", buffering=0) as controller:
            while chunk := controller.read(65536):
                self._written.extend(chunk)
                if not started and b"\n" in self._written:
                    started = True
                    self.emit(events)
                    self._responded.set()
                    if self._later is None:
                        self.end_events()
        self.end_events()

    def release_later(self) -> None:
        assert self._later is not None
        assert self._responded.wait(10), "helper peer did not receive START"
        self.emit(b"".join(self._later))
        self.end_events()

    @property
    def written(self) -> bytes:
        """Controller input, complete once the controller closed its lease."""
        if self.stdin.closed:
            self._peer.join(10)
            assert not self._peer.is_alive(), "helper peer did not observe controller EOF"
        return bytes(self._written)
