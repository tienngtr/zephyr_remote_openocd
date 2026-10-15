# SPDX-License-Identifier: Apache-2.0

"""Local child-process termination and event-driven finite commands."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO, Any, BinaryIO, Protocol

from zephyr_remote_openocd.controller._cleanup import add_failure_note
from zephyr_remote_openocd.controller.cancellation import admit_effect
from zephyr_remote_openocd.controller.waiting import WaitSources, wait_for

_PROCESS_TERM_TIMEOUT = 5.0
_PROCESS_KILL_TIMEOUT = 1.0
_READ_SIZE = 65536
_WRITE_SIZE = 65536


class Terminable(Protocol):
    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class TerminationResult:
    """Failures observed while attempting to stop a process."""

    primary_error: BaseException | None
    secondary_errors: tuple[BaseException, ...]


def terminate_process(process: Terminable) -> TerminationResult:
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

    return TerminationResult(primary_error, tuple(secondary_errors))


class _OwnedPopen(subprocess.Popen[bytes]):
    """Register the raw child on its finite owner before construction runs."""

    def __init__(self, owner: _FiniteProcess, argv: list[str], **kwargs: Any) -> None:
        # Registration precedes the effect, so a BaseException at the base
        # constructor's return cannot leave a published child unowned.
        owner.process = self
        super().__init__(argv, **kwargs)


class _FiniteProcess:
    """Own one finite child from construction until its pipes and exit settle."""

    def __init__(self, input_data: bytes | None) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.piped_input = input_data is not None
        self.input = memoryview(input_data or b"")
        self.offset = 0
        self.output: dict[int, bytearray] = {}
        self.open_reads: set[int] = set()
        self.exited = False

    def launch(self, argv: list[str], stdin: BinaryIO | None, capture_output: bool) -> None:
        """Populate this already-owned driver with the child and its pipes."""
        pipe = subprocess.PIPE if capture_output else None
        _OwnedPopen(
            self,
            argv,
            stdin=subprocess.PIPE if self.piped_input else stdin,
            stdout=pipe,
            stderr=pipe,
        )
        process = self._child()
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

    def _child(self) -> subprocess.Popen[bytes]:
        assert self.process is not None
        return self.process

    @property
    def stdin_open(self) -> bool:
        stdin = self._child().stdin
        return stdin is not None and not stdin.closed

    def captured(self, stream: IO[bytes] | None) -> bytes | None:
        return None if stream is None else bytes(self.output[stream.fileno()])

    def run(
        self, deadline: float | None, timeout: float | None
    ) -> subprocess.CompletedProcess[bytes]:
        """Return the result after EOF on every capture and observed, reaped exit."""
        process = self._child()
        while self.open_reads or self.stdin_open or not self.exited:
            ready = wait_for(
                readable=self.open_reads,
                writable=(self._stdin_fd(),) if self.stdin_open else (),
                sources=WaitSources(processes=() if self.exited else (process,)),
                deadline=deadline,
            )
            for descriptor in ready.readable:
                self._read(descriptor)
            if self.stdin_open and self._stdin_fd() in ready.writable:
                self._write()
            if process.pid in ready.exited:
                self.exited = process.poll() is not None
            if ready.expired and (self.open_reads or self.stdin_open or not self.exited):
                assert timeout is not None
                raise subprocess.TimeoutExpired(
                    process.args,
                    timeout,
                    output=self.captured(process.stdout),
                    stderr=self.captured(process.stderr),
                )
        returncode = process.returncode
        assert returncode is not None
        result = subprocess.CompletedProcess(
            process.args,
            returncode,
            self.captured(process.stdout),
            self.captured(process.stderr),
        )
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        return result

    def _stdin_fd(self) -> int:
        stdin = self._child().stdin
        assert stdin is not None
        return stdin.fileno()

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
            stdin = self._child().stdin
            assert stdin is not None
            stdin.close()

    def settle(self, primary: BaseException) -> None:
        """Stop and reap an abandoned child, retaining cleanup failures on the primary."""
        process = self.process
        if process is None or getattr(process, "pid", None) is None:
            # Construction failed before a child existed; Popen released its pipes.
            return
        streams = (process.stdin, process.stdout, process.stderr)
        if process.returncode is not None and all(
            stream is None or stream.closed for stream in streams
        ):
            # Failed exec already reaped the child and closed every pipe.
            return
        termination = terminate_process(process)
        failures = [
            *((termination.primary_error,) if termination.primary_error is not None else ()),
            *termination.secondary_errors,
        ]
        for stream in streams:
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except BaseException as error:
                    failures.append(error)
        for failure in failures:
            add_failure_note(primary, "command cleanup also failed", failure)


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
    # The resource-free driver owns the child from inside its construction.
    driver = _FiniteProcess(input_data)
    try:
        driver.launch(list(argv), stdin, capture_output)
        return driver.run(deadline, timeout)
    except BaseException as error:
        driver.settle(error)
        raise
