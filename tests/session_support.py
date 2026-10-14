# SPDX-License-Identifier: Apache-2.0

"""Compose session authority notification as ``SessionController`` does."""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest
from zephyr_remote_openocd.controller import helper_connection as connection_module
from zephyr_remote_openocd.controller.authority import SessionAuthority
from zephyr_remote_openocd.controller.waiting import Descriptor, Notifier, WaitResult, WaitSources

from tests.control_support import ControlProcess

_NO_WAIT_SOURCES = WaitSources()


@pytest.fixture(autouse=True)
def pipe_control_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pipe-only fault doubles have EOF but no OS process-completion source.

    Real-process regressions retain the full production wait. These existing
    doubles deliberately model poll/wait exceptions rather than kernel children.
    """
    real_wait = connection_module.wait_for

    def wait(
        *,
        readable: Iterable[Descriptor] = (),
        writable: Iterable[Descriptor] = (),
        sources: WaitSources = _NO_WAIT_SOURCES,
        deadline: float | None = None,
    ) -> WaitResult:
        return real_wait(
            readable=readable,
            writable=writable,
            sources=WaitSources(
                notifiers=sources.notifiers,
                processes=tuple(p for p in sources.processes if not isinstance(p, ControlProcess)),
            ),
            deadline=deadline,
        )

    monkeypatch.setattr(connection_module, "wait_for", wait)


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
