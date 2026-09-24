# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import math
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from typing import Any, BinaryIO, cast, override
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote import ssh as ssh_module
from zephyr_remote_openocd.remote.backend import RemoteSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.forwarding import _ForwardManager
from zephyr_remote_openocd.remote.helper_client import _HelperClient
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
)
from zephyr_remote_openocd.remote.protocol import encode_message
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import (
    SSH_STDERR_TAIL_BYTES,
    ManagedSshProcess,
    SshCommand,
    _stop_process,
)

LONG_LIVED_CHILD_EXIT_CODE = 7
STREAM_CHILD_EXIT_CODE = 4
SAMPLE_OPENOCD_EXIT_CODE = 6


class _PopenOnlySshCommand(SshCommand):
    @override
    def run(
        self,
        host: str,
        remote_command: str,
        *,
        input_data: bytes | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("run() is not expected in this test")

    @override
    def run_stream(
        self,
        host: str,
        remote_command: str,
        stream: BinaryIO,
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("run_stream() is not expected in this test")


def _pipe_stream(payload: bytes) -> BinaryIO:
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "wb", buffering=0) as stream:
        stream.write(payload)
    return os.fdopen(read_fd, "rb", buffering=0)


def test_fixed_arguments_are_preserved_without_a_shell():
    ssh = SshCommand(("custom-ssh", "-F", "/a file", "-o", "BatchMode=yes"))
    assert ssh.argv("board-lab", "printf marker") == [
        "custom-ssh",
        "-F",
        "/a file",
        "-o",
        "BatchMode=yes",
        "board-lab",
        "printf marker",
    ]


@patch("subprocess.Popen")
def test_long_lived_process_preserves_explicit_path_and_generated_arguments(popen):
    popen.return_value.stderr = io.BytesIO()
    SshCommand(("/opt/client/custom-ssh", "-F", "/a file")).popen("host", "serve", "-N")
    popen.assert_called_once_with(
        ["/opt/client/custom-ssh", "-F", "/a file", "-N", "host", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_bare_alternate_executable_is_resolved_through_path(tmp_path, monkeypatch):
    executable = tmp_path / "custom-ssh"
    executable.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    result = SshCommand(("custom-ssh", "--fixed")).run("target", "remote command")

    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--fixed", "target", "remote command"]


def test_long_lived_process_drains_noisy_stderr_and_keeps_bounded_tail():
    code = (
        "import sys;"
        "sys.stderr.buffer.write(b'prefix\\n' + b'x' * 200000 + b'tail-marker\\n');"
        "sys.stderr.flush();"
        "print('READY', flush=True);"
        "sys.stdin.buffer.read();"
        f"raise SystemExit({LONG_LIVED_CHILD_EXIT_CODE})"
    )
    process = SshCommand((sys.executable, "-c", code)).popen("host", "ignored")
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"READY\n"
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=5) == LONG_LIVED_CHILD_EXIT_CODE
        tail = process.stderr_tail()
        assert len(tail) <= SSH_STDERR_TAIL_BYTES
        assert tail.endswith(b"tail-marker\n")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.close_stderr()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()


def test_run_stream_passes_file_as_stdin_and_captures_output(tmp_path):
    code = (
        "import sys;"
        "sys.stdout.buffer.write(sys.stdin.buffer.read());"
        "sys.stderr.buffer.write(b'x' * 100000);"
        "sys.stderr.flush();"
        f"raise SystemExit({STREAM_CHILD_EXIT_CODE})"
    )
    source_path = tmp_path / "payload.bin"
    source_path.write_bytes(b"payload")
    with source_path.open("rb") as source:
        result = SshCommand((sys.executable, "-c", code)).run_stream("host", "ignored", source)
        assert not source.closed
    assert result.returncode == STREAM_CHILD_EXIT_CODE
    assert result.stdout == b"payload"
    assert result.stderr == b"x" * 100000


@pytest.mark.timeout(10)
def test_stderr_tail_is_best_effort_when_drain_has_not_reached_eof(monkeypatch):
    first_chunk_read = threading.Event()
    release_eof = threading.Event()

    class Stream:
        def __init__(self):
            self.reads = 0

        def read(self, _size=-1):
            self.reads += 1
            if self.reads == 1:
                first_chunk_read.set()
                return b"prefix"
            assert release_eof.wait(5)
            return b""

        def close(self):
            release_eof.set()

    drain = ssh_module._StderrDrain(cast(BinaryIO, Stream()))
    original_wait = drain._finished.wait
    monkeypatch.setattr(drain._finished, "wait", lambda _timeout=None: False)
    drain.start()
    try:
        assert first_chunk_read.wait(5)
        assert drain.tail() == b"prefix"
        assert not drain._finished.is_set()
        release_eof.set()
        assert original_wait(5)
    finally:
        release_eof.set()
        drain._thread.join(timeout=5)
        assert not drain._thread.is_alive()


@pytest.mark.timeout(10)
def test_stderr_tail_waits_for_delayed_eof_with_a_bounded_timeout(monkeypatch):
    first_chunk_read = threading.Event()
    release_suffix = threading.Event()
    suffix_read = threading.Event()

    class Stream:
        def __init__(self):
            self.reads = 0

        def read(self, _size=-1):
            self.reads += 1
            if self.reads == 1:
                first_chunk_read.set()
                return b"prefix"
            if self.reads == 2:
                assert release_suffix.wait(5)
                suffix_read.set()
                return b"suffix"
            return b""

        def close(self):
            release_suffix.set()

    monkeypatch.setattr(ssh_module, "_SSH_STDERR_JOIN_TIMEOUT", 1.0)
    drain = ssh_module._StderrDrain(cast(BinaryIO, Stream()))
    wait_timeouts = []
    original_wait = drain._finished.wait

    def wait(timeout=None):
        wait_timeouts.append(timeout)
        release_suffix.set()
        return original_wait(timeout)

    monkeypatch.setattr(drain._finished, "wait", wait)
    drain.start()
    try:
        assert first_chunk_read.wait(5)
        assert drain.tail() == b"prefixsuffix"
        assert suffix_read.is_set()
        assert wait_timeouts
        assert all(
            timeout is not None
            and math.isfinite(timeout)
            and 0 < timeout <= ssh_module._SSH_STDERR_JOIN_TIMEOUT
            for timeout in wait_timeouts
        )
    finally:
        release_suffix.set()
        drain._thread.join(timeout=5)
        assert not drain._thread.is_alive()


def test_process_cleanup_closes_an_active_stderr_drain():
    code = "import sys,time;sys.stderr.write('x' * 8192);sys.stderr.flush();time.sleep(30)"
    process = SshCommand((sys.executable, "-c", code)).popen("host", "ignored")
    try:
        _stop_process(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_process_cleanup_kills_after_graceful_termination_times_out():
    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.returncode = None
            self.terminated = False
            self.killed = False
            self.graceful_wait_timed_out = False
            self.kill_wait_completed = False
            self.stderr_closed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            assert timeout is not None and math.isfinite(timeout) and timeout > 0
            if not self.killed:
                self.graceful_wait_timed_out = True
                raise subprocess.TimeoutExpired("fake-ssh", timeout)
            self.kill_wait_completed = True
            self.returncode = -signal.SIGKILL
            return self.returncode

        def kill(self):
            self.killed = True

        def close_stderr(self):
            self.stderr_closed = True

    process = Process()

    _stop_process(cast(ManagedSshProcess, process))

    assert process.terminated
    assert process.graceful_wait_timed_out
    assert process.killed
    assert process.kill_wait_completed
    assert process.returncode is not None
    assert process.stderr_closed
    assert process.stdin.closed
    assert process.stdout.closed


class _HelperProcess:
    def __init__(self, output):
        self.stdin = io.BytesIO()
        self.stdout = _pipe_stream(output)
        self.stderr = None
        self.returncode = None
        self.args = ("fake-helper",)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -signal.SIGKILL

    def close_stderr(self):
        pass

    def stderr_tail(self):
        return b""


class _ForwardCommand(_PopenOnlySshCommand):
    processes: Iterator[Any]
    calls: list[tuple[str, str, tuple[str, ...]]]

    def __init__(self, *processes):
        super().__init__()
        object.__setattr__(self, "processes", iter(processes))
        object.__setattr__(self, "calls", [])

    @override
    def popen(self, host: str, remote_command: str, *extra_args: str) -> Any:
        self.calls.append((host, remote_command, extra_args))
        return next(self.processes)


@pytest.mark.timeout(10)
def test_initial_forward_failure_consumes_terminal_openocd_event(monkeypatch):
    terminal_seen = threading.Event()
    forward_error = SessionError("initial forwarding failed")
    helper = _HelperProcess(
        encode_message(
            "SESSION_CREATED",
            helper="fake",
            session_id="session",
            remote_workspace="/workspace",
        )
        + encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1)
        + encode_message(
            "SESSION_CLOSED", reason="process_exit", returncode=SAMPLE_OPENOCD_EXIT_CODE
        )
    )
    helper.returncode = 0
    command = _ForwardCommand(helper)
    request = RemoteSessionRequest(
        "host",
        command,
        RemoteProcess(("child",)),
        services=(Service("gdb", 3333, 3333),),
    )
    deployment = DeploymentResult("/helper.py", "digest", False)
    dispatch = _HelperClient._dispatch

    monkeypatch.setattr(backend_module, "deploy_helper", lambda *_args: deployment)
    monkeypatch.setattr(RemoteSession, "_stage", lambda _session, _files: None)

    def observe_terminal(helper_client, event):
        dispatch(helper_client, event)
        if event["type"] == "SESSION_CLOSED":
            terminal_seen.set()

    def fail_forwards(_manager, _services, _address):
        assert terminal_seen.wait(5)
        raise forward_error

    monkeypatch.setattr(_HelperClient, "_dispatch", observe_terminal)
    monkeypatch.setattr(_ForwardManager, "start", fail_forwards)

    with pytest.raises(SessionError) as raised:
        RemoteSession.open(request)

    assert raised.value is forward_error
    assert not getattr(raised.value, "__notes__", ())


def test_drain_startup_error_is_primary_when_process_cleanup_fails(monkeypatch):
    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            raise RuntimeError("process kill failed")

        def wait(self):
            return self.returncode

    process = Process()

    startup_error = RuntimeError("stderr drain startup failed")

    def fail_start(_drain):
        raise startup_error

    monkeypatch.setattr(ssh_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(ssh_module._StderrDrain, "start", fail_start)
    with pytest.raises(RuntimeError) as raised:
        SshCommand(("fake-ssh",)).popen("host", "ignored")
    assert raised.value is startup_error
    assert any("process kill failed" in note for note in raised.value.__notes__)
