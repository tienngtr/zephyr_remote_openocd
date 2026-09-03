# SPDX-License-Identifier: Apache-2.0

"""Small OpenSSH-compatible command abstraction for the SSH spike."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import BinaryIO


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

    def popen(
        self, host: str, remote_command: str | None, *extra_args: str
    ) -> subprocess.Popen[bytes]:
        """Start a long-lived SSH operation, retaining explicit lifecycle control."""
        argv = [*self.argv_prefix, *extra_args, host]
        if remote_command is not None:
            argv.append(remote_command)
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def run_stream(
        self, host: str, remote_command: str, stream: BinaryIO, *, timeout: float = 60
    ) -> subprocess.CompletedProcess[bytes]:
        """Copy a file-like payload without first materializing it as bytes."""
        process = self.popen(host, remote_command)
        assert process.stdin is not None
        try:
            shutil.copyfileobj(stream, process.stdin, length=1024 * 1024)
            process.stdin.close()
            process.stdin = None
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
