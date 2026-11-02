# SPDX-License-Identifier: Apache-2.0

"""Local ``--log-file`` sink owned by the session controller."""

from __future__ import annotations

import os
import stat

from zephyr_remote_openocd.controller._cleanup import add_failure_note
from zephyr_remote_openocd.controller.authority import SessionError


class LocalLogFile:
    """Own one validated, truncated regular file until the helper reader settles."""

    def __init__(self, path: str, descriptor: int) -> None:
        self.path = path
        self.descriptor: int | None = descriptor

    @classmethod
    def open(cls, path: str) -> LocalLogFile:
        """Open without truncation, check the type, then truncate a regular file.

        ``O_NONBLOCK`` makes a FIFO without a reader fail instead of blocking
        an open that runner Ctrl-C cannot interrupt; no type is modified
        before it has been checked.
        """
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC,
                0o666,
            )
        except OSError as error:
            raise SessionError(f"cannot open OpenOCD log file {path}: {error}") from error
        try:
            try:
                regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
                if regular:
                    os.ftruncate(descriptor, 0)
            except OSError as error:
                raise SessionError(f"cannot prepare OpenOCD log file {path}: {error}") from error
            if not regular:
                raise SessionError(f"OpenOCD log file is not a regular file: {path}")
            return cls(path, descriptor)
        except BaseException as failure:
            try:
                os.close(descriptor)
            except BaseException as cleanup_error:
                add_failure_note(failure, "log file rollback also failed", cleanup_error)
            raise

    def write(self, data: bytes) -> None:
        """Write the whole chunk; a short write continues, no progress is failure."""
        assert self.descriptor is not None
        view = memoryview(data)
        while view:
            count = os.write(self.descriptor, view)
            if count <= 0:
                raise OSError("OpenOCD log file write made no progress")
            view = view[count:]

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            os.close(descriptor)
