# SPDX-License-Identifier: Apache-2.0
"""Local launch authority and independently bounded transport shutdown."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace

from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, _stop_process

from .model import Diagnostic, Outcome, TerminalResult, failure_record, weakened
from .unix import RealTimers, Timers, readable


@dataclass(frozen=True)
class Opening:
    ready: bool = False
    forwarded: bool = False


@dataclass(frozen=True)
class Active:
    process: subprocess.Popen[bytes]


@dataclass(frozen=True)
class Cancelling:
    outcome: Outcome
    process: subprocess.Popen[bytes] | None = None


@dataclass(frozen=True)
class Ended:
    outcome: Outcome


type LocalState = Opening | Active | Cancelling | Ended


class LaunchGate:
    def __init__(self) -> None:
        self.state: LocalState = Opening()

    def ready(self) -> None:
        if isinstance(self.state, Opening):
            self.state = replace(self.state, ready=True)

    def forwarded(self) -> None:
        if isinstance(self.state, Opening):
            self.state = replace(self.state, forwarded=True)

    def cancel(self, failure: Diagnostic | None = None) -> None:
        outcome = (
            self.state.outcome
            if isinstance(self.state, (Cancelling, Ended))
            else Outcome('requested')
        )
        if failure:
            outcome = outcome.fail(failure)
        if isinstance(self.state, Ended):
            self.state = Ended(outcome)
            return
        process = self.state.process if isinstance(self.state, (Active, Cancelling)) else None
        self.state = Cancelling(outcome, process)

    def finish(self, outcome: Outcome) -> None:
        process = self.state.process if isinstance(self.state, (Active, Cancelling)) else None
        if process is not None:
            if process.poll() is None:
                raise ValueError('local dependent process is not settled')
            process.wait()
        if isinstance(self.state, (Cancelling, Ended)) and self.state.outcome.primary is not None:
            current = self.state.outcome
            if outcome.primary is not None and outcome.primary != current.primary:
                current = current.fail(outcome.primary)
            outcome = replace(
                current,
                diagnostics=(*current.diagnostics, *outcome.diagnostics),
                child=outcome.child,
            )
        self.state = Ended(outcome)

    def queue_launch(self, spawn: Callable[[], subprocess.Popen[bytes]]) -> asyncio.Future[bool]:
        result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def enter() -> None:
            eligible = isinstance(self.state, Opening) and self.state.ready and self.state.forwarded
            if eligible or weakened('local-after-cancel'):
                try:
                    self.state = Active(spawn())
                except Exception as error:
                    self.cancel(failure_record('local-launch', error))
                    result.set_exception(error)
                else:
                    result.set_result(True)
            else:
                result.set_result(False)

        asyncio.get_running_loop().call_soon(enter)
        return result


@dataclass(frozen=True)
class Shutdown:
    terminal_received: bool
    transport_status: int
    remote_cleanup_confirmed: bool
    outcome: Outcome


async def shutdown(
    process: ManagedSshProcess,
    pid: int,
    terminal: asyncio.Future[TerminalResult | None],
    *,
    timers: Timers | None = None,
    primary: Diagnostic | None = None,
) -> Shutdown:
    """Directional EOF, bounded coordination, then owned transport escalation.

    Receiving a snapshot does not settle the SSH process or its diagnostic drain.
    The same local shutdown budget covers waiting for both.
    """
    timers = timers or RealTimers()
    outcome = Outcome('requested', primary)
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError as error:
            outcome = outcome.fail(failure_record('transport', error))
    descriptor: int | None = None
    exited: asyncio.Future[None]
    if process.poll() is not None:
        exited = asyncio.get_running_loop().create_future()
        exited.set_result(None)
    else:
        descriptor = os.pidfd_open(pid)
        exited = asyncio.create_task(readable(descriptor))
    deadline = timers.arm('local-shutdown', 10)
    received = False
    confirmed = False
    try:
        done, _ = await asyncio.wait(
            (terminal, exited, deadline), return_when=asyncio.FIRST_COMPLETED
        )
        # Process exit readiness can precede the reader's final buffered frame.
        # Keep accounting that reader within the original coordination budget.
        if exited in done and not terminal.done() and not deadline.done():
            await asyncio.wait((terminal, deadline), return_when=asyncio.FIRST_COMPLETED)
        result: TerminalResult | None = None
        if terminal.done() and not terminal.cancelled():
            try:
                result = terminal.result()
            except Exception as error:
                outcome = outcome.fail(failure_record('protocol', error))
        if result is not None:
            remote = result.outcome
            received = True
            confirmed = result.disposal_confirmed
            if remote.primary is not None:
                outcome = outcome.fail(remote.primary)
            outcome = replace(
                outcome, diagnostics=(*outcome.diagnostics, *remote.diagnostics), child=remote.child
            )
        if not exited.done() and not deadline.done():
            await asyncio.wait((exited, deadline), return_when=asyncio.FIRST_COMPLETED)
        if not exited.done():
            if not received:
                confirmed = weakened('timeout-success')
            outcome = outcome.fail(
                Diagnostic('transport', 'local shutdown deadline; transport not settled')
            )
            process.terminate()
            done, _ = await asyncio.wait(
                (exited, timers.arm('local-term', 5)), return_when=asyncio.FIRST_COMPLETED
            )
            if exited not in done:
                process.kill()
                await exited  # SIGKILL/reaping OS progress assumption, not remote disposal
        status: int = process.wait()
        if status:
            outcome = outcome.fail(Diagnostic('transport', f'transport exited {status}'))
        if not received:
            confirmed = confirmed and weakened('timeout-success')
            outcome = outcome.fail(
                Diagnostic('transport', 'no terminal result; disposal unconfirmed')
            )
        try:
            _stop_process(process)
        except Exception as error:
            outcome = outcome.fail(failure_record('cleanup', error))
        return Shutdown(received, status, confirmed, outcome)
    finally:
        exited.cancel()
        await asyncio.gather(exited, return_exceptions=True)
        deadline.cancel()
        if descriptor is not None:
            os.close(descriptor)
