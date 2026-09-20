# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import os
import sys
from collections.abc import Iterator
from contextlib import suppress
from types import SimpleNamespace
from typing import Any, cast, override

import pytest
from zephyr_remote_openocd.remote import forwarding as forwarding_module
from zephyr_remote_openocd.remote.forwarding import _ForwardManager
from zephyr_remote_openocd.remote.model import Service
from zephyr_remote_openocd.remote.session import SessionError
from zephyr_remote_openocd.remote.ssh import SSH_STDERR_TAIL_BYTES, SshCommand

FORWARD_FAILURE_RC = 13


class _ForwardCommand(SshCommand):
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


def test_forward_manager_raises_when_ssh_forward_exits():
    class Forward:
        def poll(self):
            return FORWARD_FAILURE_RC

        def stderr_tail(self):
            return b"forward failed"

    manager = _ForwardManager(SshCommand(), "host")
    manager._processes = [cast(Any, Forward())]

    with pytest.raises(SessionError):
        manager.check_health()


@pytest.mark.timeout(5)
def test_forward_readiness_timeout_does_not_block_on_partial_output(monkeypatch):
    read_fd, write_fd = os.pipe()

    class Process:
        def __init__(self):
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)

        @staticmethod
        def poll():
            return None

    process = Process()
    try:
        os.write(write_fd, b"ZRO_FORWARD_")

        class Clock:
            now = 0.0

            def monotonic(self):
                return self.now

        class Selector:
            def __init__(self):
                self.delivered = False

            def register(self, _stream, _events):
                pass

            def select(self, _timeout):
                if not self.delivered:
                    self.delivered = True
                    clock.now = 1.0
                    return [(None, None)]
                return []

            def close(self):
                pass

        clock = Clock()
        monkeypatch.setattr(forwarding_module.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(forwarding_module.selectors, "DefaultSelector", Selector)
        deadline = 0.5
        assert not _ForwardManager._await_ready(cast(Any, process), "ZRO_FORWARD_ready", deadline)
    finally:
        process.stdout.close()
        os.close(write_fd)


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
        diagnostic = _ForwardManager._diagnostic(process)
        assert diagnostic.endswith("forward-tail")
        assert len(diagnostic.encode()) <= SSH_STDERR_TAIL_BYTES
    finally:
        process.close_stderr()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()


def test_initial_start_forward_failure_associates_all_preflight_advisories_with_services(
    monkeypatch,
):
    first_port, second_port = 32133, 32144
    _patch_preflight_socket(monkeypatch, {first_port, second_port})
    first = Service("tcl", first_port, 6333)
    second = Service("telnet", second_port, 4444)
    command = _ForwardCommand(_ForwardProcess(7))
    manager = _ForwardManager(command, "host")
    try:
        with pytest.raises(SessionError) as raised:
            manager.start((first, second), "127.64.0.1")

        message = str(raised.value)
        assert f"127.0.0.1:{first_port} for tcl" in message
        assert f"127.0.0.1:{second_port} for telnet" in message
        assert command.calls[0][2] == (
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"127.0.0.1:{first_port}:127.64.0.1:6333",
        )
    finally:
        with suppress(BaseException):
            manager.close()


def test_dynamic_forward_failure_identifies_service_and_local_port(monkeypatch):
    port = 32155
    _patch_preflight_socket(monkeypatch, set())
    service = Service("rtt", port, 5555)
    manager = _ForwardManager(_ForwardCommand(_ForwardProcess(9)), "host")
    try:
        with pytest.raises(SessionError) as raised:
            manager.start((service,), "127.64.0.1")

        message = str(raised.value)
        assert service.name in message
        assert f"127.0.0.1:{port}" in message
    finally:
        manager.close()


def test_dynamic_forward_timeout_identifies_service_and_local_port(monkeypatch):
    port = 32166
    _patch_preflight_socket(monkeypatch, set())
    service = Service("rtt", port, 5555)
    manager = _ForwardManager(_ForwardCommand(_ForwardProcess(None)), "host")
    monkeypatch.setattr(_ForwardManager, "_await_ready", lambda *_args: False)

    try:
        with pytest.raises(SessionError) as raised:
            manager.start((service,), "127.64.0.1")

        message = str(raised.value)
        assert service.name in message
        assert f"127.0.0.1:{port}" in message
    finally:
        manager.close()


def test_forward_manager_rejects_a_service_already_forwarded(monkeypatch):
    service = Service("gdb", 32177, 3333)
    command = _ForwardCommand(_ForwardProcess(None))
    manager = _ForwardManager(command, "host")
    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    monkeypatch.setattr(_ForwardManager, "_await_ready", staticmethod(lambda *_args: True))

    try:
        manager.start((service,), "127.64.0.1")
        with pytest.raises(SessionError, match="service names must remain unique"):
            manager.start((service,), "127.64.0.1")
        assert len(command.calls) == 1
    finally:
        manager.close()


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

    def stderr_tail(self):
        return b""

    def close_stderr(self):
        pass


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
        forwarding_module,
        "socket",
        SimpleNamespace(socket=lambda: _PreflightSocket(occupied)),
    )
