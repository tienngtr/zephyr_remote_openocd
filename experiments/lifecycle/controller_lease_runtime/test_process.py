# SPDX-License-Identifier: Apache-2.0
"""Real helper process, output status provenance and local coordination."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, _stop_process

from .harness import STANDIN, JsonReader
from .local import shutdown
from .model import ChildResult, Diagnostic, Outcome, TerminalResult
from .unix import readable


def decode(frame: dict[str, object]) -> TerminalResult:
    def child_result(value: object) -> ChildResult | None:
        if value is None:
            return None
        assert isinstance(value, dict)
        return ChildResult(value['generation'], value['returncode'])

    def diagnostic(value: object) -> Diagnostic | None:
        if value is None:
            return None
        assert isinstance(value, dict)
        children = tuple(diagnostic(child) for child in value['details'])
        assert all(child is not None for child in children)
        return Diagnostic(
            value['source'],
            value['message'],
            tuple(c for c in children if c),
            child_result(value.get('child')),
        )

    child = frame['child']
    result = None
    if child is not None:
        assert isinstance(child, dict)
        result = ChildResult(child['generation'], child['returncode'])
    diagnostics = frame['diagnostics']
    assert isinstance(diagnostics, list)
    secondary = tuple(diagnostic(value) for value in diagnostics)
    assert all(value is not None for value in secondary)
    return TerminalResult(
        Outcome(
            str(frame['trigger']),
            diagnostic(frame['primary']),
            tuple(value for value in secondary if value),
            result,
        ),
        frame['disposal_confirmed'] is True,
    )


@pytest.mark.parametrize('cause', ('eof', 'SIGINT', 'SIGTERM', 'flash-fail'))
def test_helper_final_result_and_real_process_status(tmp_path: Path, cause: str) -> None:
    async def scenario() -> None:
        workspace = tmp_path / 'remote-workspace'
        child = subprocess.Popen(
            [
                sys.executable,
                '-u',
                '-m',
                'experiments.lifecycle.controller_lease_runtime.helper',
                '--workspace',
                str(workspace),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        managed = ManagedSshProcess.from_popen(child)
        assert managed.stdout is not None and managed.stdin is not None
        frames = JsonReader(managed.stdout.fileno())
        terminal: asyncio.Future[TerminalResult | None] = asyncio.get_running_loop().create_future()
        recorded: list[dict[str, object]] = []

        async def observe() -> None:
            try:
                while True:
                    frame = await frames.read()
                    recorded.append(frame)
                    if frame['type'] == 'SESSION_ENDED':
                        terminal.set_result(decode(frame))
                        return
            except EOFError:
                terminal.set_result(None)

        try:
            assert (await frames.read())['type'] == 'SESSION_CREATED'
            profile = 'flash-fail' if cause == 'flash-fail' else 'ready'
            argv = [sys.executable, '-u', str(STANDIN), '--profile', profile]
            managed.stdin.write(
                (
                    json.dumps(
                        {
                            'type': 'START',
                            'argv': argv,
                            'required': [] if cause == 'flash-fail' else ['INIT', 'STARTUP'],
                            'policy': 'exit' if cause == 'flash-fail' else 'live',
                            'max_attempts': 1,
                        }
                    )
                    + '\n'
                ).encode()
            )
            managed.stdin.flush()
            if cause != 'flash-fail':
                while (await frames.read())['type'] != 'READY':
                    pass
            observer = asyncio.create_task(observe())
            if cause.startswith('SIG'):
                os.kill(child.pid, getattr(signal, cause))
            if cause == 'flash-fail':
                await terminal
            result = await shutdown(managed, child.pid, terminal)
            await observer
            assert result.terminal_received and result.remote_cleanup_confirmed
            assert result.transport_status == 0
            assert not workspace.exists()
            assert sum(frame['type'] == 'SESSION_ENDED' for frame in recorded) == 1
            if cause == 'flash-fail':
                assert result.outcome.child == ChildResult(1, 7)
                assert result.outcome.primary is not None
                assert result.outcome.primary.source == 'openocd'
            else:
                assert result.outcome.child is None  # cleanup wait is not a natural result
        finally:
            _stop_process(managed)

    asyncio.run(scenario())


def test_helper_status_after_valid_terminal_is_not_child_status() -> None:
    async def scenario() -> None:
        frame: dict[str, object] = {
            'type': 'SESSION_ENDED',
            'trigger': 'controller-ended',
            'primary': None,
            'diagnostics': [],
            'child': None,
            'disposal_confirmed': True,
        }
        child = subprocess.Popen(
            [
                sys.executable,
                '-u',
                '-c',
                f'import sys; print({json.dumps(json.dumps(frame))}); sys.exit(5)',
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        managed = ManagedSshProcess.from_popen(child)
        assert managed.stdout is not None
        frames = JsonReader(managed.stdout.fileno())
        terminal: asyncio.Future[TerminalResult | None] = asyncio.get_running_loop().create_future()
        try:
            terminal.set_result(decode(await frames.read()))
            descriptor = os.pidfd_open(child.pid)
            try:
                await readable(descriptor)
            finally:
                os.close(descriptor)
            result = await shutdown(managed, child.pid, terminal)
            assert result.remote_cleanup_confirmed and result.terminal_received
            assert result.transport_status == 5
            assert result.outcome.child is None
            assert result.outcome.primary is not None
            assert result.outcome.primary.source == 'transport'
        finally:
            _stop_process(managed)

    asyncio.run(scenario())


def test_zero_helper_status_without_terminal_is_failure_not_child_success() -> None:
    async def scenario() -> None:
        child = subprocess.Popen(
            [sys.executable, '-c', 'pass'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        managed = ManagedSshProcess.from_popen(child)
        terminal: asyncio.Future[TerminalResult | None] = asyncio.get_running_loop().create_future()
        assert managed.stdout is not None
        reader = JsonReader(managed.stdout.fileno())
        try:
            with pytest.raises(EOFError):
                await reader.read()
            terminal.set_result(None)
            managed.wait(timeout=30)  # already reaped transport must not need its PID again
            result = await shutdown(managed, child.pid, terminal)
            assert result.transport_status == 0 and result.outcome.child is None
            assert not result.remote_cleanup_confirmed and not result.terminal_received
            assert result.outcome.primary is not None
        finally:
            _stop_process(managed)

    asyncio.run(scenario())
