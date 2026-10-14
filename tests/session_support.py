# SPDX-License-Identifier: Apache-2.0

"""Compose session authority notification as ``SessionController`` does."""

from __future__ import annotations

from collections.abc import Callable

from zephyr_remote_openocd.controller.authority import SessionAuthority
from zephyr_remote_openocd.controller.waiting import Notifier, WaitSources


class NotifiedAuthority:
    """A session authority whose state changes wake its coordinator's notifier."""

    def __init__(self) -> None:
        self.notifier = Notifier()
        self.notifier.open()
        self.authority = SessionAuthority(notify=self.notifier.notify)

    def close(self) -> None:
        self.notifier.close()


class ObservedStatus:
    """A session whose result producers publish state, then notify."""

    def __init__(self, result: Callable[[], int | None]) -> None:
        self.notifier = Notifier()
        self.notifier.open()
        self._result = result

    def poll_operation(self) -> int | None:
        return self._result()

    def observation_sources(self) -> WaitSources:
        return WaitSources(notifiers=(self.notifier,))

    def changed(self) -> None:
        self.notifier.notify()

    def close(self) -> None:
        self.notifier.close()
