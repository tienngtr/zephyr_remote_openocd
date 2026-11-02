# SPDX-License-Identifier: Apache-2.0

"""Helper-owned readiness artifact, read only by the session coordinator."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from zephyr_remote_openocd.protocol.process import READINESS_ARTIFACT
from zephyr_remote_openocd.protocol.wire import MAX_FRAME_SIZE

# One bounded read of the artifact per observation (SAD §40).
MAX_READINESS_ARTIFACT_BYTES = MAX_FRAME_SIZE
READINESS_POLL_INTERVAL = 0.05


class ReadinessArtifact:
    """Own the read descriptor of a fresh, empty, owner-only evidence file."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor: int | None = descriptor

    @classmethod
    def create(cls, work: Path) -> ReadinessArtifact:
        descriptor = os.open(
            work / READINESS_ARTIFACT,
            os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            # The umask could remove the owner write bit OpenOCD needs.
            os.fchmod(descriptor, 0o600)
            return cls(descriptor)
        except BaseException as failure:
            try:
                os.close(descriptor)
            except BaseException as cleanup_error:
                failure.add_note(f"readiness artifact rollback also failed: {cleanup_error}")
            raise

    def observe(self, names: Iterable[str]) -> frozenset[str]:
        """Return requested names that occur as complete LF-terminated lines."""
        assert self.descriptor is not None
        data = os.pread(self.descriptor, MAX_READINESS_ARTIFACT_BYTES, 0)
        # The final element is an unterminated line, or empty; it never counts.
        lines = set(data.split(b"\n")[:-1])
        return frozenset(name for name in names if name.encode("ascii") in lines)

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            os.close(descriptor)
