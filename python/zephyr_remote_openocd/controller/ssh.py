# SPDX-License-Identifier: Apache-2.0

"""Small OpenSSH-compatible command abstraction."""

from __future__ import annotations

import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Protocol, cast

from zephyr_remote_openocd.controller.cleanup import _add_failure_note, _raise_cleanup_errors

# SSH diagnostics must never be allowed to fill the OS pipe, but retaining a
# small tail keeps connection and forwarding failures actionable.  This limit
# applies per long-lived SSH process and is deliberately independent of the
# amount of diagnostic output produced by the client.
SSH_STDERR_TAIL_BYTES = 64 * 1024
_SSH_STDERR_READ_BYTES = 8192
_SSH_STDERR_JOIN_TIMEOUT = 1.0
_PROCESS_TERM_TIMEOUT = 5.0
_PROCESS_KILL_TIMEOUT = 1.0


class SshProcessStartError(RuntimeError):
    """Preserve the rollback outcome when managed-process acquisition fails."""

    def __init__(self, cause: Exception, cleanup_errors: tuple[BaseException, ...]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.cleanup_errors = cleanup_errors
        for note in getattr(cause, "__notes__", ()):
            self.add_note(note)


class _StderrDrain:
    """Consume a process stderr pipe while retaining a bounded byte tail."""

    def __init__(self, stream: BinaryIO):
        self._stream = stream
        self._tail = bytearray()
        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="zro-ssh-stderr",
            daemon=True,
        )
        self._close_lock = threading.Lock()
        self._started = False
        self._stream_closed = False

    def start(self) -> None:
        """Start consuming the owned stderr stream."""
        self._thread.start()
        self._started = True

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(_SSH_STDERR_READ_BYTES)
                if not chunk:
                    return
                self._append(bytes(chunk))
        except BaseException:
            # Closing a pipe during process cleanup is an expected way to
            # release a blocked reader.  Diagnostics are best effort.
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

        Closing a buffered pipe from a different thread can wait for that
        reader's internal lock.  If the process (or a descendant) still owns
        the write side, waiting for the reader to finish is not safe to turn
        into a synchronous ``close()``.  Report a bounded cleanup failure and
        leave the stream alone when its reader has not reached EOF.
        """
        with self._close_lock:
            if self._stream_closed:
                return
            if self._started:
                deadline = time.monotonic() + _SSH_STDERR_JOIN_TIMEOUT
                if not self._finished.wait(max(0.0, deadline - time.monotonic())):
                    raise TimeoutError("SSH stderr reader did not stop; stream retained")
                self._thread.join(max(0.0, deadline - time.monotonic()))
                if self._thread.is_alive():
                    raise TimeoutError("SSH stderr reader did not stop; stream retained")
            self._stream.close()
            self._stream_closed = True


class ManagedSshProcess:
    """Long-lived SSH process with an explicitly owned stderr drain."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._drain: _StderrDrain | None = None

    def acquire(self, argv: list[str]) -> None:
        """Populate an already-owned transport; its caller owns rollback."""
        assert self._process is None
        self._process = _OwnedSshPopen(self, argv)
        assert self._process.stderr is not None
        self._drain = _StderrDrain(cast(BinaryIO, self._process.stderr))
        self._drain.start()
        self._process.stderr = None

    def _rollback_acquisition(self, error: BaseException) -> None:
        if self._process is None or getattr(self._process, "pid", None) is None:
            return
        termination = _terminate_process(self)
        cleanup_errors = (
            *((termination.primary_error,) if termination.primary_error is not None else ()),
            *termination.secondary_errors,
            *_close_process_streams(self, close_streams=True),
        )
        for cleanup_failure in cleanup_errors:
            _add_failure_note(error, "SSH process startup cleanup failed", cleanup_failure)
        if isinstance(error, (OSError, subprocess.SubprocessError)):
            raise SshProcessStartError(error, cleanup_errors) from error

    def _process_or_error(self) -> subprocess.Popen[bytes]:
        assert self._process is not None
        return self._process

    @property
    def args(self):
        return self._process_or_error().args

    @property
    def stdin(self):
        return None if self._process is None else self._process.stdin

    @property
    def stdout(self):
        return None if self._process is None else self._process.stdout

    @property
    def returncode(self):
        return None if self._process is None else self._process.returncode

    def poll(self):
        return self._process_or_error().poll()

    def wait(self, timeout=None):
        return self._process_or_error().wait(timeout=timeout)

    def terminate(self):
        self._process_or_error().terminate()

    def kill(self):
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
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


class _ProcessControl(Protocol):
    """Process operations and resources needed during SSH cleanup."""

    @property
    def stdin(self) -> BinaryIO | None: ...

    @property
    def stdout(self) -> BinaryIO | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def close_stderr(self) -> None: ...


@dataclass(frozen=True)
class _TerminationResult:
    """Failures observed while attempting to stop a process."""

    primary_error: BaseException | None
    secondary_errors: tuple[BaseException, ...]


def _terminate_process(process: _ProcessControl) -> _TerminationResult:
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


def _close_process_streams(
    process: _ProcessControl, *, close_streams: bool
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


def _stop_process(process: _ProcessControl, *, close_streams: bool = True) -> None:
    """Stop and clean up one managed SSH process without hiding errors."""
    termination = _terminate_process(process)
    cleanup_errors = [
        *termination.secondary_errors,
        *_close_process_streams(process, close_streams=close_streams),
    ]
    if termination.primary_error is not None:
        for cleanup_failure in cleanup_errors:
            _add_failure_note(
                termination.primary_error,
                "process cleanup also failed",
                cleanup_failure,
            )
        raise termination.primary_error
    _raise_cleanup_errors(cleanup_errors)


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
        return subprocess.run(
            self.argv(host, remote_command),
            input=input_data,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def popen(
        self, host: str, remote_command: str, *, local_forward: SshForwardSpec | None = None
    ) -> ManagedSshProcess:
        """Start a long-lived SSH operation, retaining explicit lifecycle control."""
        # OpenSSH uses the first value of these options. Runner-owned forwarding
        # requirements must precede fixed user arguments and SSH configuration.
        forward_args = local_forward.argv() if local_forward is not None else ()
        argv = [self.argv_prefix[0], *forward_args, *self.argv_prefix[1:], host, remote_command]
        # Keep the controlling terminal and foreground group for SSH prompts.
        # The child inherits this thread's blocked SIGINT across exec; changing
        # a process-wide handler or running Python in preexec_fn is unsafe here.
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
                    _add_failure_note(
                        acquisition_failure, "SSH signal-mask restoration also failed", error
                    )
            return managed_process
        except BaseException as error:
            # Unmasking can deliver a pending Ctrl-C after acquisition. The
            # caller has not received the process yet, so rollback belongs here.
            managed_process._rollback_acquisition(error)
            raise

    def run_stream(
        self, host: str, remote_command: str, stream: BinaryIO, *, timeout: float = 60
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a finite SSH command with a file-backed payload as stdin."""
        return subprocess.run(
            self.argv(host, remote_command),
            stdin=stream,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
