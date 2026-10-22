# SPDX-License-Identifier: Apache-2.0

"""Small OpenSSH-compatible command abstraction."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, BinaryIO, Protocol

from zephyr_remote_openocd.controller._cleanup import add_failure_note, raise_cleanup_errors
from zephyr_remote_openocd.controller._processes import run_finite, terminate_process
from zephyr_remote_openocd.controller.cancellation import admit_effect
from zephyr_remote_openocd.controller.waiting import Notifier, WaitSources, wait_for

if TYPE_CHECKING:
    from _typeshed import StrOrBytesPath

# SSH diagnostics must never be allowed to fill the OS pipe, but retaining a
# small tail keeps connection and forwarding failures actionable.  This limit
# applies per long-lived SSH process and is deliberately independent of the
# amount of diagnostic output produced by the client.
SSH_STDERR_TAIL_BYTES = 64 * 1024
_SSH_STDERR_READ_BYTES = 8192
_SSH_STDERR_JOIN_TIMEOUT = 1.0
# Ignored SIGINT survives exec; a handler would not. Some OpenSSH releases
# unblock an inherited SIGINT mask, so long-lived transports start with both.
# The configured argv is exec'd unchanged in this process, without a shell.
_SIGINT_IGNORING_EXEC = (
    "import os, signal, sys\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "try:\n"
    "    os.execvp(sys.argv[1], sys.argv[1:])\n"
    "except OSError as error:\n"
    "    sys.stderr.write(f'cannot execute SSH command {sys.argv[1]!r}: {error.strerror}\\n')\n"
    "    sys.exit(127)\n"
)


class SshProcessStartError(RuntimeError):
    """Preserve the rollback outcome when managed-process acquisition fails."""

    def __init__(self, cause: Exception, cleanup_errors: tuple[BaseException, ...]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.cleanup_errors = cleanup_errors
        for note in getattr(cause, "__notes__", ()):
            self.add_note(note)


class StderrDrain:
    """Consume a process stderr pipe while retaining a bounded byte tail."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._tail = bytearray()
        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._stop = threading.Event()
        # Resource-free until start(); close() owns it from then on.
        self._wakeup = Notifier()
        self._thread = threading.Thread(
            target=self._run,
            name="zro-ssh-stderr",
            daemon=True,
        )
        self._close_lock = threading.Lock()
        self._stream_closed = False

    def start(self) -> None:
        """Start consuming the owned stderr stream."""
        self._wakeup.open()
        self._thread.start()

    def _run(self) -> None:
        try:
            # Raw reads after readiness never hold the buffered stream's lock,
            # and a stop request wakes the wait without waiting for EOF.
            descriptor = self._stream.fileno()
            while not self._stop.is_set():
                ready = wait_for(
                    readable=(descriptor,), sources=WaitSources(notifiers=(self._wakeup,))
                )
                if descriptor not in ready.readable:
                    continue
                chunk = os.read(descriptor, _SSH_STDERR_READ_BYTES)
                if not chunk:
                    return
                self._append(chunk)
        except BaseException:
            # Diagnostics are best effort; a failed wait or read ends the drain.
            return
        finally:
            self._finished.set()

    def _append(self, chunk: bytes) -> None:
        with self._lock:
            self._tail.extend(chunk)
            excess = len(self._tail) - SSH_STDERR_TAIL_BYTES
            if excess > 0:
                del self._tail[:excess]

    def tail(self) -> bytes:
        """Return the captured diagnostic tail after a bounded drain wait.

        If stderr has not reached EOF by the end of the bounded wait, the
        returned diagnostic is an explicitly best-effort partial tail rather
        than a completeness claim.
        """
        self._finished.wait(_SSH_STDERR_JOIN_TIMEOUT)
        with self._lock:
            return bytes(self._tail)

    def close(self) -> None:
        """Close the pipe after its reader has stopped, within a finite budget.

        The reader first gets a bounded opportunity to reach EOF so final
        diagnostics are retained. A writer outside the runner, such as a
        connection-sharing master holding passed client stdio, can keep the
        pipe open after the SSH process exits; the reader is then stopped
        without EOF. Only a reader that does not stop retains the stream,
        because closing it from this thread is not safe while it is in use.
        """
        with self._close_lock:
            if self._stream_closed:
                return
            # A started thread has an identity before start() returns, so a
            # failure at that return still leaves the reader's shutdown bounded.
            if self._thread.ident is not None:
                if not self._finished.wait(_SSH_STDERR_JOIN_TIMEOUT):
                    self._stop.set()
                    self._wakeup.notify()
                deadline = time.monotonic() + _SSH_STDERR_JOIN_TIMEOUT
                if not self._finished.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("SSH stderr reader did not stop; stream retained")
                self._thread.join(max(0.0, deadline - time.monotonic()))
                if self._thread.is_alive():
                    raise TimeoutError("SSH stderr reader did not stop; stream retained")
            cleanup_errors: list[BaseException] = []
            for release in (self._stream.close, self._wakeup.close):
                try:
                    release()
                except BaseException as error:
                    cleanup_errors.append(error)
            self._stream_closed = True
            raise_cleanup_errors(cleanup_errors)


class ManagedSshProcess:
    """Long-lived SSH process with an explicitly owned stderr drain."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._drain: StderrDrain | None = None
        self._argv: list[str] = []

    def acquire(self, argv: list[str]) -> None:
        """Populate an already-owned transport; its caller owns rollback."""
        assert self._process is None
        self._argv = argv
        self._process = _OwnedSshPopen(self, argv)
        assert self._process.stderr is not None
        self._drain = StderrDrain(self._process.stderr)
        self._drain.start()
        self._process.stderr = None

    def _rollback_acquisition(self, error: BaseException) -> None:
        if self._process is None or getattr(self._process, "pid", None) is None:
            return
        # Failed fork/exec initialization can already reap the child and close
        # all pipes. With no drain to settle, preserve that original launch error.
        if (
            self._drain is None
            and self._process.returncode is not None
            and all(
                stream is None or stream.closed
                for stream in (self._process.stdin, self._process.stdout, self._process.stderr)
            )
        ):
            return
        termination = terminate_process(self)
        cleanup_errors = (
            *((termination.primary_error,) if termination.primary_error is not None else ()),
            *termination.secondary_errors,
            *_close_process_streams(self, close_streams=True),
        )
        for cleanup_failure in cleanup_errors:
            add_failure_note(error, "SSH process startup cleanup failed", cleanup_failure)
        if isinstance(error, (OSError, subprocess.SubprocessError)):
            raise SshProcessStartError(error, cleanup_errors) from error

    def _process_or_error(self) -> subprocess.Popen[bytes]:
        assert self._process is not None
        return self._process

    @property
    def args(self) -> StrOrBytesPath | Sequence[StrOrBytesPath]:
        """The configured SSH argv, not its exec launcher."""
        self._process_or_error()
        return self._argv

    @property
    def pid(self) -> int:
        return self._process_or_error().pid

    @property
    def stdin(self) -> IO[bytes] | None:
        return None if self._process is None else self._process.stdin

    @property
    def stdout(self) -> IO[bytes] | None:
        return None if self._process is None else self._process.stdout

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    def poll(self) -> int | None:
        return self._process_or_error().poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._process_or_error().wait(timeout=timeout)

    def terminate(self) -> None:
        self._process_or_error().terminate()

    def kill(self) -> None:
        self._process_or_error().kill()

    def stderr_tail(self) -> bytes:
        assert self._drain is not None
        return self._drain.tail()

    def close_stderr(self) -> None:
        if self._drain is not None:
            self._drain.close()
        elif self._process is not None and self._process.stderr is not None:
            self._process.stderr.close()


class _OwnedSshPopen(subprocess.Popen[bytes]):
    """Register the raw owner before initialization can create a subprocess."""

    def __init__(self, owner: ManagedSshProcess, argv: list[str]) -> None:
        # Registration precedes the effect. A BaseException at the base
        # constructor's return cannot prevent rollback from finding this object.
        owner._process = self
        super().__init__(
            [sys.executable, "-I", "-S", "-c", _SIGINT_IGNORING_EXEC, *argv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


class ProcessControl(Protocol):
    """Process operations and resources needed during SSH cleanup."""

    @property
    def stdin(self) -> IO[bytes] | None: ...

    @property
    def stdout(self) -> IO[bytes] | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def close_stderr(self) -> None: ...


def _close_process_streams(
    process: ProcessControl, *, close_streams: bool
) -> tuple[BaseException, ...]:
    """Close the stderr drain and optionally the process data streams."""
    cleanup_errors: list[BaseException] = []

    try:
        process.close_stderr()
    except BaseException as error:
        cleanup_errors.append(error)
    if close_streams:
        for stream in (process.stdin, process.stdout):
            if stream is None or stream.closed:
                continue
            try:
                stream.close()
            except BaseException as error:
                cleanup_errors.append(error)
    return tuple(cleanup_errors)


def stop_process(process: ProcessControl, *, close_streams: bool = True) -> None:
    """Stop and clean up one managed SSH process without hiding errors."""
    termination = terminate_process(process)
    cleanup_errors = [
        *termination.secondary_errors,
        *_close_process_streams(process, close_streams=close_streams),
    ]
    if termination.primary_error is not None:
        for cleanup_failure in cleanup_errors:
            add_failure_note(
                termination.primary_error,
                "process cleanup also failed",
                cleanup_failure,
            )
        raise termination.primary_error
    raise_cleanup_errors(cleanup_errors)


@dataclass(frozen=True)
class SshForwardSpec:
    """Runner-owned local forwarding, including mandatory SSH semantics."""

    local_port: int
    remote_address: str
    remote_port: int

    def argv(self) -> tuple[str, ...]:
        return (
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ClearAllForwardings=no",
            "-L",
            f"127.0.0.1:{self.local_port}:{self.remote_address}:{self.remote_port}",
        )


# Receives a launched process inside acquisition rollback, so the caller owns
# it before popen() returns.
type ProcessAcquiredHandler = Callable[[ManagedSshProcess], None]


@dataclass(frozen=True)
class SshCommand:
    """Build and execute SSH argv without shell interpretation."""

    argv_prefix: tuple[str, ...] = ("ssh",)

    def __post_init__(self) -> None:
        if not self.argv_prefix or not self.argv_prefix[0]:
            raise ValueError("SSH command must contain a non-empty executable")

    def argv(self, host: str, remote_command: str) -> list[str]:
        if not host:
            raise ValueError("SSH host must not be empty")
        return [*self.argv_prefix, host, remote_command]

    def run(
        self,
        host: str,
        remote_command: str,
        *,
        input_data: bytes | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a finite SSH command; without input, the child inherits stdin."""
        return run_finite(self.argv(host, remote_command), input_data=input_data, timeout=timeout)

    def popen(
        self,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired: ProcessAcquiredHandler | None = None,
    ) -> ManagedSshProcess:
        """Start SSH and register caller ownership inside rollback before returning."""
        # OpenSSH uses the first value of these options. Runner-owned forwarding
        # requirements must precede fixed user arguments and SSH configuration.
        forward_args = local_forward.argv() if local_forward is not None else ()
        argv = [self.argv_prefix[0], *forward_args, *self.argv_prefix[1:], host, remote_command]
        # Keep the controlling terminal and foreground group for SSH prompts.
        # The mask is child signal topology, not parent protection: the child
        # inherits blocked SIGINT across exec, and the exec launcher adds an
        # ignored disposition, so terminal Ctrl-C meant for an interactive
        # client does not end the long-lived transport.
        admit_effect()
        managed_process = ManagedSshProcess()
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        acquisition_failure: BaseException | None = None
        try:
            try:
                # Raw process and drain acquisition populate this existing
                # owner. Construction rollback needs neither a new wrapper nor
                # a successfully constructed drain.
                managed_process.acquire(argv)
            except BaseException as error:
                acquisition_failure = error
                raise
            finally:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                except BaseException as error:
                    if acquisition_failure is None:
                        raise
                    add_failure_note(
                        acquisition_failure, "SSH signal-mask restoration also failed", error
                    )
            if on_acquired is not None:
                on_acquired(managed_process)
            return managed_process
        except BaseException as error:
            # The caller has not received the process yet, so rollback of any
            # failure after acquisition, including registration, belongs here.
            managed_process._rollback_acquisition(error)
            raise

    def run_stream(
        self, host: str, remote_command: str, stream: BinaryIO, *, timeout: float = 60
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a finite SSH command with a file-backed payload as stdin."""
        return run_finite(self.argv(host, remote_command), stdin=stream, timeout=timeout)
