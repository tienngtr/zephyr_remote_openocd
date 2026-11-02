# SPDX-License-Identifier: Apache-2.0

"""Helper-owned OpenOCD log channel and its bounded, exact-prefix relay.

The coordinator calls this owner from dispatch. It holds the channel
descriptors, the relay buffer, LOG_DATA admission, and the log decision, and
records deferred log facts. It never commits or changes a lifecycle transition.
"""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path

from zephyr_remote_openocd.helper import _streams as streams
from zephyr_remote_openocd.helper._cleanup import raise_cleanup_errors
from zephyr_remote_openocd.protocol.outcome import Diagnostic, LogReport
from zephyr_remote_openocd.protocol.wire import MAX_LOG_DATA_BYTES

LOG_CHANNEL = "log"
LOG_RELAY_BUFFER_BYTES = 8 * 1024 * 1024
# Encoded LOG_DATA admitted to the protocol writer but not yet written.
LOG_CREDIT_BYTES = 256 * 1024
# Absolute final-drain bound after the remote termination commit.
LOG_DRAIN_DEADLINE = 10


class LogRelay:
    """Relay the session log channel as a contiguous prefix of its byte stream."""

    def __init__(self, path: Path, read_descriptor: int, write_descriptor: int) -> None:
        self.path = path
        self.read_descriptor: int | None = read_descriptor
        # The keep-open writer: end-of-file can mark the final extent only
        # after it closes, which follows confirmed process-group disposal.
        self._writer: int | None = write_descriptor
        self._buffer: deque[bytes] = deque()
        self._buffered = 0
        self.admitted = 0
        self._continuity_lost = False
        self._end_of_file = False
        self._reading = True
        # The final phase: end-of-file, confirmed disposal, or the cut. A
        # reader that stops earlier, as after an active read failure, does
        # not authorize the decision before termination disposes of OpenOCD.
        self._final_phase = False
        self.disposal_confirmed = False
        self._incomplete: Diagnostic | None = None
        self.decision: LogReport | None = None

    @classmethod
    def create(cls, work: Path) -> LogRelay:
        path = work / LOG_CHANNEL
        os.mkfifo(path, 0o600)
        # The umask could remove the owner write bit OpenOCD needs.
        os.chmod(path, 0o600, follow_symlinks=False)
        # The read end exists first, so opening the writer cannot block.
        reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            return cls(path, reader, writer)
        except BaseException as failure:
            try:
                os.close(reader)
            except BaseException as cleanup_error:
                failure.add_note(f"log channel rollback also failed: {cleanup_error}")
            raise

    @property
    def descriptors_open(self) -> bool:
        return self.read_descriptor is not None or self._writer is not None

    def _record_incomplete(self, code: str, message: str) -> None:
        """Retain the first cause; later causes cannot replace it."""
        if self._incomplete is None and self.decision is None:
            self._incomplete = Diagnostic(code, message)

    def _lose_continuity(self) -> None:
        # After the first byte that is not admitted, no later byte is admitted.
        self._continuity_lost = True
        self._buffer.clear()
        self._buffered = 0

    def accept(self, chunk: bytes) -> None:
        """Buffer channel bytes; excess is discarded so reading never stops."""
        if not chunk:
            self._end_of_file = True
            self._reading = False
            self._final_phase = True
            return
        if self._continuity_lost or self.decision is not None:
            return
        room = LOG_RELAY_BUFFER_BYTES - self._buffered
        if len(chunk) > room:
            if room:
                self._buffer.append(chunk[:room])
                self._buffered += room
            self._continuity_lost = True
            self._record_incomplete(
                "LOG_RELAY_OVERFLOW", "log relay buffer overflowed; later log bytes were discarded"
            )
            return
        self._buffer.append(chunk)
        self._buffered += len(chunk)

    def pump(self) -> None:
        """Admit buffered bytes as LOG_DATA while the writer's log credit allows."""
        output = streams.protocol_output.get()
        while self._buffer and (output is None or output.log_pending < LOG_CREDIT_BYTES):
            data = self._buffer.popleft()
            if len(data) > MAX_LOG_DATA_BYTES:
                self._buffer.appendleft(data[MAX_LOG_DATA_BYTES:])
                data = data[:MAX_LOG_DATA_BYTES]
            self._buffered -= len(data)
            try:
                streams.emit_log(self.admitted, data)
            except BaseException as error:
                self._lose_continuity()
                self._record_incomplete("LOG_DATA_ADMISSION", f"log data was not admitted: {error}")
                return
            self.admitted += len(data)

    def confirm_disposal(self) -> None:
        """The process group is gone: close the keep-open writer to expose the final extent."""
        self.disposal_confirmed = True
        self._final_phase = True
        writer, self._writer = self._writer, None
        if writer is not None:
            os.close(writer)

    def cut(self) -> None:
        """Disposal is unconfirmed: stop reading; buffered bytes may still be delivered."""
        self._reading = False
        self._final_phase = True
        self._record_incomplete(
            "LOG_DISPOSAL_UNCONFIRMED",
            "OpenOCD process disposal is unconfirmed; the log channel was not read to its end",
        )

    def read_failed(self, error: BaseException) -> None:
        self._reading = False
        self._record_incomplete("LOG_READ_FAILURE", f"log channel read failed: {error}")

    @property
    def ready_to_decide(self) -> bool:
        return (
            self.decision is None and self._final_phase and not self._reading and not self._buffer
        )

    def decide(self, *, deadline: bool = False, child_acquired: bool = True) -> None:
        """Make the one log decision; complete only for the whole channel byte stream."""
        if self.decision is not None:
            return
        if deadline and (self._buffer or self._reading):
            self._record_incomplete(
                "LOG_DRAIN_DEADLINE", "final log delivery did not finish before its deadline"
            )
        if child_acquired and not (self._end_of_file and self.disposal_confirmed):
            self._record_incomplete(
                "LOG_RELAY_UNSETTLED", "the log channel was not read to its final extent"
            )
        self._lose_continuity()
        self.decision = LogReport(self.admitted, self._incomplete is None, self._incomplete)

    def report(self, *, child_disposal_confirmed: bool) -> LogReport:
        """Project the decision; completeness requires confirmed child disposal."""
        if self.decision is None:
            self.decide()
        assert self.decision is not None
        if self.decision.complete and not child_disposal_confirmed:
            return LogReport(
                self.decision.byte_count,
                False,
                Diagnostic(
                    "LOG_DISPOSAL_UNCONFIRMED",
                    "OpenOCD process disposal is unconfirmed; the log relay cannot be complete",
                ),
            )
        return self.decision

    def close(self) -> None:
        """Close every relay descriptor, attempting each independently.

        Linux releases a descriptor even when close reports an error, so a
        failed close is reported but never retried against a reused number.
        """
        errors: list[BaseException] = []
        for descriptor in (self._writer, self.read_descriptor):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except BaseException as error:
                errors.append(error)
        self._writer = self.read_descriptor = None
        raise_cleanup_errors(errors)
