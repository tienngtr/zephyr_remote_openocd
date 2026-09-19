# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote import ssh as ssh_module
from zephyr_remote_openocd.remote.backend import SshHelperSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.model import RemoteProcess, RemoteSessionRequest, Service
from zephyr_remote_openocd.remote.protocol import encode_message
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import SSH_STDERR_TAIL_BYTES, SshCommand


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
        "raise SystemExit(7)"
    )
    process = SshCommand((sys.executable, "-c", code)).popen("host", "ignored")
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"READY\n"
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(timeout=5) == 7
        tail = process.stderr_tail(wait=True)
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
        "raise SystemExit(4)"
    )
    source_path = tmp_path / "payload.bin"
    source_path.write_bytes(b"payload")
    with source_path.open("rb") as source:
        result = SshCommand((sys.executable, "-c", code)).run_stream("host", "ignored", source)
        assert not source.closed
    assert result.returncode == 4
    assert result.stdout == b"payload"
    assert result.stderr == b"x" * 100000


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
            release_eof.wait()
            return b""

        def close(self):
            release_eof.set()

    monkeypatch.setattr(ssh_module, "_SSH_STDERR_JOIN_TIMEOUT", 0)
    drain = ssh_module._StderrDrain(Stream())
    drain.start()
    try:
        assert first_chunk_read.wait(timeout=1)
        assert drain.tail(wait=True) == b"prefix"
        assert not drain._finished.is_set()
        release_eof.set()
        assert drain._finished.wait(timeout=1)
    finally:
        release_eof.set()
        drain._thread.join(timeout=1)


def test_process_cleanup_closes_an_active_stderr_drain():
    code = "import sys,time;sys.stderr.write('x' * 8192);sys.stderr.flush();time.sleep(30)"
    process = SshCommand((sys.executable, "-c", code)).popen("host", "ignored")
    try:
        SshHelperSession._stop_process(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_helper_output_delivery_does_not_retain_event_history():
    payloads = [f"payload-{index}" for index in range(1024)]
    frames = [
        encode_message(
            "SESSION_CREATED",
            helper="fake",
            session_id="session",
            remote_workspace="/workspace",
        ),
        encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1),
        *(
            encode_message(
                "CHILD_OUTPUT",
                stream="stdout" if index % 2 == 0 else "stderr",
                payload=payload,
                line_end=False,
            )
            for index, payload in enumerate(payloads)
        ),
        encode_message("SESSION_CLOSED", reason="process_exit", returncode=0),
    ]

    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"".join(frames))
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
            self.returncode = -9

        def stderr_tail(self, *, wait=False):
            return b""

        def close_stderr(self):
            pass

    class Command:
        def __init__(self):
            self.process = Process()

        def popen(self, host, remote_command, *extra_args):
            return self.process

    handled = []
    backend = SshHelperSession(
        RemoteSessionRequest(
            "host",
            Command(),
            process=RemoteProcess(("child",)),
        ),
        DeploymentResult("/helper.py", "digest", False),
        1,
        lambda stream, payload, line_end: handled.append((stream, payload, line_end)),
    )
    try:
        backend.start(())
        assert backend.reader_thread is not None
        backend.reader_thread.join(timeout=1)
        assert not backend.reader_thread.is_alive()
        assert handled == [
            ("stdout" if index % 2 == 0 else "stderr", payload, False)
            for index, payload in enumerate(payloads)
        ]
        assert backend.poll() == 0
        assert "events" not in vars(backend)
    finally:
        backend.close()


def test_forward_diagnostic_keeps_a_useful_tail_after_nonzero_exit():
    code = (
        "import sys;"
        "sys.stderr.buffer.write(b'x' * 200000 + b'forward-tail\\n');"
        "sys.stderr.flush();"
        "raise SystemExit(9)"
    )
    process = SshCommand((sys.executable, "-c", code)).popen("host", "ignored")
    try:
        assert process.wait(timeout=5) == 9
        diagnostic = SshHelperSession._forward_diagnostic(process)
        assert diagnostic.endswith("forward-tail")
        assert len(diagnostic.encode()) <= SSH_STDERR_TAIL_BYTES
    finally:
        process.close_stderr()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()


class _ForwardProcess:
    def __init__(self, returncode):
        self.stdin = io.BytesIO()
        self.stdout = None
        self.stderr = None
        self.returncode = returncode
        self.args = ("fake-forward",)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def stderr_tail(self, *, wait=False):
        return b""

    def close_stderr(self):
        pass


class _HelperProcess:
    def __init__(self, output):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
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
        self.returncode = -9


class _ForwardCommand:
    def __init__(self, *processes):
        self.processes = iter(processes)
        self.calls = []

    def popen(self, host, remote_command, *extra_args):
        self.calls.append((host, remote_command, extra_args))
        return next(self.processes)


def _forward_session(command, *, timeout=1):
    session = object.__new__(SshHelperSession)
    session.request = SimpleNamespace(host="host", ssh_command=command)
    session.forward_start_timeout = timeout
    session.forwards = []
    session.closed = False
    session.descriptor = SimpleNamespace(remote_address="127.64.0.1")
    session.output_handler = None
    session.process_returncode = None
    session.reader_error = None
    session.reader_thread = None
    session._terminal_reason = None
    session._state_lock = threading.RLock()
    return session


class _PreflightSocket:
    def __init__(self, occupied):
        self.occupied = occupied

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return False

    def bind(self, address):
        assert address[0] == "127.0.0.1"
        if address[1] in self.occupied:
            raise OSError("address already in use")


def _patch_preflight_socket(monkeypatch, occupied):
    monkeypatch.setattr(
        backend_module,
        "socket",
        SimpleNamespace(socket=lambda: _PreflightSocket(occupied)),
    )


def test_initial_start_forward_failure_associates_all_preflight_advisories_with_services(
    monkeypatch,
):
    first_port, second_port = 32133, 32144
    _patch_preflight_socket(monkeypatch, {first_port, second_port})
    first = Service("tcl", first_port, 6333)
    second = Service("telnet", second_port, 4444)
    command = _ForwardCommand(
        _HelperProcess(
            encode_message(
                "SESSION_CREATED",
                helper="fake",
                session_id="session",
                remote_workspace="/workspace",
            )
            + encode_message("PROCESS_READY", remote_address="127.64.0.1", child_pid=1)
        ),
        _ForwardProcess(7),
    )
    backend = SshHelperSession(
        RemoteSessionRequest("host", command),
        DeploymentResult("/helper.py", "digest", False),
        1,
    )
    try:
        with pytest.raises(SessionError) as raised:
            backend.start((first, second))

        message = str(raised.value)
        assert f"127.0.0.1:{first_port} for tcl" in message
        assert f"127.0.0.1:{second_port} for telnet" in message
        assert f"SSH forwarding failed for tcl on 127.0.0.1:{first_port}" in message
        assert command.calls[1][2] == (
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"127.0.0.1:{first_port}:127.64.0.1:6333",
        )
    finally:
        with suppress(BaseException):
            backend.close()


def test_dynamic_forward_failure_identifies_service_and_local_port(monkeypatch):
    port = 32155
    _patch_preflight_socket(monkeypatch, {port})
    service = Service("rtt", port, 5555)
    session = _forward_session(_ForwardCommand(_ForwardProcess(9)))
    with pytest.raises(SessionError) as raised:
        session.forward((service,))

    message = str(raised.value)
    assert f"127.0.0.1:{port} for rtt" in message
    assert f"SSH forwarding failed for rtt on 127.0.0.1:{port}" in message
    assert session.forwards == []


def test_dynamic_forward_timeout_identifies_service_and_local_port(monkeypatch):
    port = 32166
    _patch_preflight_socket(monkeypatch, set())
    service = Service("rtt", port, 5555)
    session = _forward_session(_ForwardCommand(_ForwardProcess(None)))
    monkeypatch.setattr(SshHelperSession, "_await_forward_ready", lambda *_args: False)

    with pytest.raises(SessionError) as raised:
        session.forward((service,))

    assert str(raised.value) == (f"SSH forwarding did not become ready for rtt on 127.0.0.1:{port}")
    assert session.forwards == []


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

    def fail_start(_drain):
        raise RuntimeError("stderr drain startup failed")

    monkeypatch.setattr(ssh_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(ssh_module._StderrDrain, "start", fail_start)
    with pytest.raises(RuntimeError, match="stderr drain startup failed") as raised:
        SshCommand(("fake-ssh",)).popen("host", "ignored")
    assert any("process kill failed" in note for note in raised.value.__notes__)
