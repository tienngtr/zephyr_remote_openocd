# SPDX-License-Identifier: Apache-2.0

"""Compose session authority notification as ``SessionController`` does."""

from __future__ import annotations

from zephyr_remote_openocd.controller.authority import SessionAuthority
from zephyr_remote_openocd.controller.waiting import Notifier


class NotifiedAuthority:
    """A session authority whose state changes wake its coordinator's notifier."""

    def __init__(self) -> None:
        self.notifier = Notifier()
        self.notifier.open()
        self.authority = SessionAuthority(notify=self.notifier.notify)

    def close(self) -> None:
        self.notifier.close()
