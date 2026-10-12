# SPDX-License-Identifier: Apache-2.0

"""Native signal observation and synchronous lifecycle commit scopes."""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType


class _SessionSignals:
    """Own installed session handlers, including partially completed setup."""

    def __init__(self) -> None:
        self.previous_handlers: dict[
            int, Callable[[int, FrameType | None], object] | int | None
        ] = {}
        self.pending_signum: int | None = None

    def capture(self, signum: int) -> bool:
        """Latch only a native fact, without mutating asyncio or lifecycle state."""
        if self.pending_signum is not None:
            return False
        self.pending_signum = signum
        return True

    def install(self, handler: Callable[[int, FrameType | None], None]) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)

    def restore(self) -> Iterator[BaseException]:
        """Report each failed restoration before attempting the next handler."""
        for signum, handler in self.previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as exc:
                yield exc

    @contextmanager
    def commit(self) -> Iterator[None]:
        """Serialize native signal delivery with a synchronous lifecycle commit."""
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            yield
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
