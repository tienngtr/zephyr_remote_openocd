# SPDX-License-Identifier: Apache-2.0

"""Deadline-aware reads for test-owned pipes (do not mix with buffered reads)."""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import IO

from zephyr_remote_openocd.remote.cleanup import _add_failure_note

PROCESS_CLEANUP_TIMEOUT = 10


class ProcessOutputMonitor:
    """Continuously capture a process pipe and signal observable output."""

    def __init__(self, stream: IO[bytes] | IO[str]):
        self._stream = stream
        self._fd = stream.fileno()
        self._output = bytearray()
        self._condition = threading.Condition()
        self._finished = False
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            while chunk := os.read(self._fd, 4096):
                with self._condition:
                    self._output.extend(chunk)
                    self._condition.notify_all()
        except BaseException as error:
            with self._condition:
                self._error = error
        finally:
            with self._condition:
                self._finished = True
                self._condition.notify_all()

    @property
    def text(self) -> str:
        with self._condition:
            return bytes(self._output).decode("utf-8", "replace")

    def wait_for(self, pattern: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        encoded = pattern.encode()
        with self._condition:
            while encoded not in self._output:
                if self._error is not None:
                    raise AssertionError(
                        f"process output read failed: {self._error}"
                    ) from self._error
                if self._finished:
                    raise AssertionError(
                        f"process output ended before {pattern!r} was observed:\n{self.text}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"pattern {pattern!r} not observed before timeout:\n{self.text}"
                    )
                self._condition.wait(remaining)

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise AssertionError("process output reader did not terminate")
        try:
            if self._error is not None:
                raise AssertionError(f"process output read failed: {self._error}") from self._error
        finally:
            self._stream.close()


def _raise_process_cleanup_errors(errors: list[BaseException]) -> None:
    if errors:
        first, *later = errors
        for error in later:
            first.add_note(f"additional process cleanup failure: {error}")
        raise first


class ProcessScope:
    """Own an isolated command group, its pipes, and an optional output reader."""

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self._output: ProcessOutputMonitor | None = None
        self._stopped = False
        self._closed = False

    def capture_output(self) -> ProcessOutputMonitor:
        assert self._output is None and self.process.stdout is not None
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            self._output = ProcessOutputMonitor(self.process.stdout)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return self._output

    @property
    def output_text(self) -> str:
        return self._output.text if self._output is not None else ""

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        errors: list[BaseException] = []
        try:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGINT)
                self.process.wait(timeout=PROCESS_CLEANUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        except BaseException as error:
            errors.append(error)
        # An exited leader does not prove that its group or pipe writers exited.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except BaseException as error:
            errors.append(error)
        try:
            self.process.wait(timeout=PROCESS_CLEANUP_TIMEOUT)
        except BaseException as error:
            errors.append(error)
        _raise_process_cleanup_errors(errors)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        try:
            self.stop()
        except BaseException as error:
            errors.append(error)
        if self._output is not None:
            try:
                self._output.join(timeout=PROCESS_CLEANUP_TIMEOUT)
            except BaseException as error:
                errors.append(error)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except BaseException as error:
                    errors.append(error)
        _raise_process_cleanup_errors(errors)


@contextmanager
def managed_process(args, **kwargs) -> Iterator[ProcessScope]:
    """Acquire and clean up a command without replacing an existing failure."""
    owner = None
    primary = None
    try:
        # Defer the parent's handler rather than masking: exec restores caught
        # handlers, while an inherited blocked SIGINT would prevent graceful stop.
        interrupted = False

        def defer_interrupt(signum, frame):
            nonlocal interrupted
            interrupted = True

        previous_handler = signal.signal(signal.SIGINT, defer_interrupt)
        try:
            owner = ProcessScope(subprocess.Popen(args, start_new_session=True, **kwargs))
        except BaseException as error:
            if interrupted:
                error.add_note("SIGINT also received during hardware process acquisition")
            raise
        finally:
            signal.signal(signal.SIGINT, previous_handler)
        if interrupted:
            signal.raise_signal(signal.SIGINT)
        yield owner
    except BaseException as error:
        primary = error
        raise
    finally:
        if owner is not None:
            cleanup_errors: list[BaseException] = []
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            try:
                try:
                    owner.close()
                except BaseException as error:
                    cleanup_errors.append(error)
            finally:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                except BaseException as error:
                    cleanup_errors.append(error)
            if primary is not None:
                if owner.output_text:
                    primary.add_note("hardware process output:\n" + owner.output_text)
                for cleanup_error in cleanup_errors:
                    primary.add_note(f"hardware process cleanup also failed: {cleanup_error}")
                    for note in getattr(cleanup_error, "__notes__", ()):
                        primary.add_note(note)
            else:
                _raise_process_cleanup_errors(cleanup_errors)


def run_process(args, *, timeout: float, check: bool = False, **kwargs):
    """Blocking hardware command with bounded group and pipe cleanup."""
    with managed_process(args, **kwargs) as owner:
        stdout, stderr = owner.process.communicate(timeout=timeout)
        result = subprocess.CompletedProcess(args, owner.process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def assert_semihosting_acceptance(returncode: int | None, output: str, pattern: str) -> None:
    """Require natural command success and the configured semihosting output."""
    assert returncode == 0, output
    assert re.search(pattern, output), output


@contextmanager
def cleanup_on_exit(cleanup: Callable[[], None]) -> Iterator[None]:
    """Attempt owned test cleanup without replacing an active failure."""
    primary = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            cleanup()
        except BaseException as error:
            if primary is None:
                raise
            _add_failure_note(primary, "test cleanup also failed", error)


def read_line(stream, timeout=30):
    """Read one binary line without prefetching bytes needed by communicate()."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise AssertionError(f"output line timed out; partial output: {bytes(data)!r}")
            chunk = os.read(stream.fileno(), 1)
            data.extend(chunk)
            if not chunk or chunk == b"\n":
                return bytes(data)


def read_lines(stream, timeout=30):
    """Read through EOF under one deadline, including continuously chatty children."""
    deadline = time.monotonic() + timeout
    while line := read_line(stream, deadline - time.monotonic()):
        yield line


def read_until(process, pattern, timeout, output):
    """Match accumulated output before waiting for additional pipe data."""
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while not re.search(pattern.encode(), output):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                break
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            output.extend(chunk)
        else:
            return
    raise AssertionError(
        f"pattern {pattern!r} not observed; status={process.poll()}:\n"
        + bytes(output).decode("utf-8", "replace")
    )
