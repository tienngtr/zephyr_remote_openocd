# SPDX-License-Identifier: Apache-2.0
"""Separate process-shared staging lease, independent of child/output decisions."""

from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .model import Cleaned, Diagnostic
from .unix import Timers, bounded, readable


class Workspace:
    def __init__(self, path: Path, released_fd: int | None = None) -> None:
        self.path = path
        self.lease = path.parent / (path.name + '.lease')
        self.closed = path.parent / (path.name + '.closed')
        self.released_fd = released_fd
        path.mkdir(mode=0o700)
        try:
            self.lease.touch(mode=0o600)
        except BaseException as failure:
            try:
                shutil.rmtree(path)
            except BaseException as rollback:
                raise BaseExceptionGroup(
                    'workspace allocation rollback', [failure, rollback]
                ) from None
            raise

    @contextmanager
    def staging(self) -> Iterator[Path]:
        with self.lease.open('r+b') as lease:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if self.closed.exists() or not self.path.is_dir():
                raise ValueError('workspace is closing')
            yield self.path

    async def cleanup(
        self, timers: Timers, predecessors: tuple[asyncio.Task[Cleaned], ...] = ()
    ) -> tuple[Diagnostic, ...]:
        try:
            return await self._cleanup(timers, predecessors)
        except OSError as error:
            return (Diagnostic('cleanup', str(error)),)

    async def _cleanup(
        self, timers: Timers, predecessors: tuple[asyncio.Task[Cleaned], ...]
    ) -> tuple[Diagnostic, ...]:
        self.closed.touch(mode=0o600)
        # Close admission now, retain staged input until its physical users have
        # disposed. This wait cannot block the independent child cleanup workers.
        if predecessors:
            results = await asyncio.gather(*predecessors, return_exceptions=True)
            if any(isinstance(result, BaseException) or not result.disposed for result in results):
                return (Diagnostic('cleanup', 'workspace retained: child disposal unconfirmed'),)
        with self.lease.open('r+b') as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if self.released_fd is None:
                    # No unbounded lock wait; real staging needs a release
                    # notification or bounded polling in its separate owner.
                    return (Diagnostic('cleanup', 'workspace staging lease is still held'),)
                response = asyncio.create_task(readable(self.released_fd))
                try:
                    if not await bounded(response, timers.arm('workspace', 10)):
                        return (Diagnostic('cleanup', 'workspace stage deadline'),)
                    os.read(self.released_fd, 1)
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError) as error:
                    return (Diagnostic('cleanup', str(error)),)
                finally:
                    response.cancel()
                    await asyncio.gather(response, return_exceptions=True)
            try:
                shutil.rmtree(self.path)
            except OSError as error:
                return (Diagnostic('cleanup', str(error)),)
        # Keep closed admission as a tombstone in the experimental parent,
        # not a recreated lease that a delayed stage could open.
        return ()
