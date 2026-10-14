# SPDX-License-Identifier: Apache-2.0

"""Local child-process termination and event-driven finite commands."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO, BinaryIO, Protocol

from zephyr_remote_openocd.controller.cancellation import admit_effect
from zephyr_remote_openocd.controller.cleanup import _add_failure_note
from zephyr_remote_openocd.controller.waiting import WaitSources, wait_for

_PROCESS_TERM_TIMEOUT = 5.0
_PROCESS_KILL_TIMEOUT = 1.0
_READ_SIZE = 65536
_WRITE_SIZE = 65536


class _Terminable(Protocol):
    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class _TerminationResult:
    """Failures observed while attempting to stop a process."""

    primary_error: BaseException | None
    secondary_errors: tuple[BaseException, ...]


def _terminate_process(process: _Terminable) -> _TerminationResult:
    """Stop one process, escalating from graceful termination to a kill."""
    primary_error: BaseException | None = None
    secondary_errors: list[BaseException] = []
    graceful_timeout: BaseException | None = None
    process_dead = False

    def record_process_error(error: BaseException) -> None:
        nonlocal primary_error
        if primary_error is None:
            primary_error = error
        else:
            secondary_errors.append(error)

    try:
        process_dead = process.poll() is not None
    except BaseException as error:
        record_process_error(error)

    if not process_dead:
        termination_failed = False
        try:
            process.terminate()
        except BaseException as error:
            termination_failed = True
            record_process_error(error)
        if termination_failed:
            try:
                process_dead = process.poll() is not None
            except BaseException as error:
                record_process_error(error)
        else:
            try:
                process.wait(timeout=_PROCESS_TERM_TIMEOUT)
                process_dead = True
            except subprocess.TimeoutExpired as error:
                graceful_timeout = error
            except BaseException as error:
                record_process_error(error)

        if not process_dead:
            kill_failed = False
            try:
                process.kill()
            except BaseException as error:
                kill_failed = True
                record_process_error(error)
            if not kill_failed:
                try:
                    process.wait(timeout=_PROCESS_KILL_TIMEOUT)
                    process_dead = True
                except BaseException as error:
                    record_process_error(error)
            if not process_dead:
                try:
                    process_dead = process.poll() is not None
                except BaseException as error:
                    record_process_error(error)

    if not process_dead and primary_error is None:
        record_process_error(RuntimeError("process did not exit during cleanup"))
    if graceful_timeout is not None and primary_error is not None:
        secondary_errors.append(graceful_timeout)

    return _TerminationResult(primary_error, tuple(secondary_errors))


class _FiniteProcess:
    """Drive one owned child's pipes and completion until both are settled."""

    def __init__(self, process: subprocess.Popen[bytes], input_data: bytes | None) -> None:
        self.process = process
        self.input = memoryview(input_data or b"")
        self.offset = 0
        self.output = {
            stream.fileno(): bytearray()
            for stream in (process.stdout, process.stderr)
            if stream is not None
        }
        self.open_reads = set(self.output)
        for descriptor in self.open_reads:
            os.set_blocking(descriptor, False)
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            if not self.input:
                process.stdin.close()
        self.exited = False

    @property
    def stdin_open(self) -> bool:
        return self.process.stdin is not None and not self.process.stdin.closed

    def captured(self, stream: IO[bytes] | None) -> bytes | None:
        return None if stream is None else bytes(self.output[stream.fileno()])

    def run(self, deadline: float | None, timeout: float | None) -> int:
        """Return the reaped status after EOF on every capture and observed exit."""
        while self.open_reads or self.stdin_open or not self.exited:
            ready = wait_for(
                readable=self.open_reads,
                writable=(self._stdin_fd(),) if self.stdin_open else (),
                sources=WaitSources(processes=() if self.exited else (self.process,)),
                deadline=deadline,
            )
            for descriptor in ready.readable:
                self._read(descriptor)
            if self.stdin_open and self._stdin_fd() in ready.writable:
                self._write()
            if self.process.pid in ready.exited:
                self.exited = self.process.poll() is not None
            if ready.expired and (self.open_reads or self.stdin_open or not self.exited):
                assert timeout is not None
                stdout = self.captured(self.process.stdout)
                stderr = self.captured(self.process.stderr)
                raise subprocess.TimeoutExpired(
                    self.process.args, timeout, output=stdout, stderr=stderr
                )
        returncode = self.process.returncode
        assert returncode is not None
        return returncode

    def _stdin_fd(self) -> int:
        assert self.process.stdin is not None
        return self.process.stdin.fileno()

    def _read(self, descriptor: int) -> None:
        try:
            chunk = os.read(descriptor, _READ_SIZE)
        except BlockingIOError:
            return
        if chunk:
            self.output[descriptor].extend(chunk)
        else:
            self.open_reads.discard(descriptor)

    def _write(self) -> None:
        assert self.process.stdin is not None
        try:
            self.offset += os.write(
                self._stdin_fd(), self.input[self.offset : self.offset + _WRITE_SIZE]
            )
        except BlockingIOError:
            return
        except BrokenPipeError:
            # Like communicate(), a reader that exits early ends input delivery.
            self.offset = len(self.input)
        if self.offset >= len(self.input):
            self.process.stdin.close()


def _settle_finite(process: subprocess.Popen[bytes], primary: BaseException) -> None:
    """Stop and reap an abandoned command, retaining cleanup failures on the primary."""
    termination = _terminate_process(process)
    failures = [
        *((termination.primary_error,) if termination.primary_error is not None else ()),
        *termination.secondary_errors,
    ]
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except BaseException as error:
                failures.append(error)
    for failure in failures:
        _add_failure_note(primary, "command cleanup also failed", failure)


def run_finite(
    argv: Sequence[str],
    *,
    input_data: bytes | None = None,
    stdin: BinaryIO | None = None,
    capture_output: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a finite command with cancellation-aware, deadlocking-free pipe I/O.

    ``input_data`` is delivered through a pipe followed by EOF; otherwise
    ``stdin`` (or the inherited descriptor) becomes the child's input directly.
    Captured streams drain concurrently. Expiry of the absolute command
    deadline raises ``TimeoutExpired``. Every exit path reaps the child; an
    interrupted command is stopped and never replayed.
    """
    admit_effect()
    deadline = None if timeout is None else time.monotonic() + timeout
    pipe = subprocess.PIPE if capture_output else None
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if input_data is not None else stdin,
        stdout=pipe,
        stderr=pipe,
    )
    try:
        driver = _FiniteProcess(process, input_data)
        returncode = driver.run(deadline, timeout)
        stdout = driver.captured(process.stdout)
        stderr = driver.captured(process.stderr)
    except BaseException as error:
        _settle_finite(process, error)
        raise
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    return subprocess.CompletedProcess(process.args, returncode, stdout, stderr)
