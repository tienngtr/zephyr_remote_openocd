# SPDX-License-Identifier: Apache-2.0

"""Small OpenSSH-compatible command abstraction."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, cast

# SSH diagnostics must never be allowed to fill the OS pipe, but retaining a
# small tail keeps connection and forwarding failures actionable.  This limit
# applies per long-lived SSH process and is deliberately independent of the
# amount of diagnostic output produced by the client.
SSH_STDERR_TAIL_BYTES = 64 * 1024
_SSH_STDERR_READ_BYTES = 8192
_SSH_STDERR_JOIN_TIMEOUT = 1.0


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
            # Closing a pipe during process disposal is an expected way to
            # release a blocked reader.  Diagnostics are best effort.
            return
        finally:
            self._finished.set()

    def _append(self, chunk: bytes) -> None:
        with self._lock:
            if len(chunk) >= SSH_STDERR_TAIL_BYTES:
                self._tail[:] = chunk[-SSH_STDERR_TAIL_BYTES:]
                return
            excess = len(self._tail) + len(chunk) - SSH_STDERR_TAIL_BYTES
            if excess > 0:
                del self._tail[:excess]
            self._tail.extend(chunk)

    def tail(self, *, wait: bool = False) -> bytes:
        """Return the captured diagnostic tail.

        ``wait=True`` waits only for the bounded drain-completion budget.  If
        stderr has not reached EOF by then, the returned diagnostic is an
        explicitly best-effort partial tail rather than a completeness claim.
        """
        if wait:
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

    def __init__(self, process: subprocess.Popen[bytes], drain: _StderrDrain):
        self._process = process
        self._drain = drain

    @classmethod
    def from_popen(cls, process: subprocess.Popen[bytes]) -> ManagedSshProcess:
        """Take ownership of a long-lived process's stderr pipe."""
        assert process.stderr is not None
        drain = _StderrDrain(cast(BinaryIO, process.stderr))
        try:
            drain.start()
            process.stderr = None
            return cls(process, drain)
        except BaseException as error:
            cleanup_errors: list[BaseException] = []
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            try:
                drain.close()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            for cleanup_failure in cleanup_errors:
                error.add_note(f"SSH process startup cleanup failed: {cleanup_failure}")
            try:
                for stream in (process.stdin, process.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()
            except BaseException as cleanup_error:
                error.add_note(f"SSH process stream cleanup failed: {cleanup_error}")
            raise

    @property
    def args(self):
        return self._process.args

    @property
    def stdin(self):
        return self._process.stdin

    @property
    def stdout(self):
        return self._process.stdout

    @property
    def returncode(self):
        return self._process.returncode

    def poll(self):
        return self._process.poll()

    def wait(self, timeout=None):
        return self._process.wait(timeout=timeout)

    def terminate(self):
        self._process.terminate()

    def kill(self):
        self._process.kill()

    def stderr_tail(self, *, wait: bool = False) -> bytes:
        return self._drain.tail(wait=wait)

    def close_stderr(self) -> None:
        self._drain.close()


@dataclass(frozen=True)
class SshCommand:
    """Build and execute SSH argv without shell interpretation."""

    argv_prefix: tuple[str, ...] = ("ssh",)

    def __post_init__(self) -> None:
        if not self.argv_prefix or not all(self.argv_prefix):
            raise ValueError("SSH command must contain at least one non-empty argument")

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

    def popen(self, host: str, remote_command: str | None, *extra_args: str) -> ManagedSshProcess:
        """Start a long-lived SSH operation, retaining explicit lifecycle control."""
        argv = [*self.argv_prefix, *extra_args, host]
        if remote_command is not None:
            argv.append(remote_command)
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return ManagedSshProcess.from_popen(process)

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
