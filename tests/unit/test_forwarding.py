# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import os
import shlex
import signal
import subprocess
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
from zephyr_remote_openocd.remote.ssh import (
    SSH_STDERR_TAIL_BYTES,
    ManagedSshProcess,
    SshCommand,
    SshLocalForward,
    _stop_process,
)

FORWARD_FAILURE_RC = 13
SAMPLE_FORWARD_EXIT_CODE = 9

# The Any/cast uses in this module are confined to the subprocess boundary.
# _ForwardManager owns the full ManagedSshProcess lifecycle, so introducing a
# narrower production protocol would only accommodate tests.  These doubles
# provide deterministic poll results, diagnostics, and partial readiness input;
# normal stderr-drain behavior uses a real SshCommand process below, and full
# cleanup ownership is covered with real managed processes in local integration.


class _ForwardCommand(SshCommand):
    processes: Iterator[Any]
    calls: list[tuple[str, str, SshLocalForward | None]]

    def __init__(self, *processes):
        super().__init__()
        object.__setattr__(self, "processes", iter(processes))
        object.__setattr__(self, "calls", [])

    @override
    def popen(
        self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
    ) -> Any:
        self.calls.append((host, remote_command, local_forward))
        return next(self.processes)


def test_forward_health_reports_service_identity_once(monkeypatch):
    services = (Service("gdb", 32100, 3333), Service("tcl", 32101, 6333))
    processes = (_ForwardProcess(None), _ForwardProcess(None))
    manager = _ForwardManager(_ForwardCommand(*processes), "host")
    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    monkeypatch.setattr(_ForwardManager, "_await_ready", staticmethod(lambda *_args: True))
    manager.start(services, "127.64.0.1")
    for process in processes:
        process.returncode = FORWARD_FAILURE_RC
    failures = manager.check_health()
    assert tuple(failure.service for failure in failures) == services
    assert all(failure.returncode == FORWARD_FAILURE_RC for failure in failures)
    assert all(failure.diagnostic == "" for failure in failures)
    assert manager.check_health() == ()
    assert manager.services == services
    manager.close()
    assert [process.close_stderr_calls for process in processes] == [1, 1]


@pytest.mark.parametrize("rollback_fails", (False, True))
def test_failed_start_exposes_rollback_outcome(monkeypatch, rollback_fails):
    service = Service("tcl", 32101, 6333)
    cleanup_error = RuntimeError("rollback cleanup failed")
    process = _ForwardProcess(None, close_stderr_error=cleanup_error if rollback_fails else None)
    manager = _ForwardManager(_ForwardCommand(process), "host")
    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    monkeypatch.setattr(_ForwardManager, "_await_ready", staticmethod(lambda *_args: False))
    with pytest.raises(SessionError) as raised:
        manager.start((service,), "127.64.0.1")
    error = raised.value
    assert isinstance(error, forwarding_module.ForwardStartError)
    assert error.service == service
    assert service.name in str(error)
    assert f"127.0.0.1:{service.local_port}" in str(error)
    assert isinstance(error.cause, SessionError)
    assert error.cleanup_errors == ((cleanup_error,) if rollback_fails else ())
    assert not manager.has_forwards
    manager.close()
    assert process.terminate_calls == 1
    assert process.close_stderr_calls == 1


def test_process_creation_failure_has_service_identity_without_ownership(monkeypatch):
    cause = OSError("cannot start configured SSH client")

    class Command(_ForwardCommand):
        @override
        def popen(
            self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
        ) -> Any:
            raise cause

    service = Service("tcl", 32101, 6333)
    manager = _ForwardManager(Command(), "host")
    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    with pytest.raises(SessionError) as raised:
        manager.start((service,), "127.64.0.1")
    assert isinstance(raised.value, forwarding_module.ForwardStartError)
    assert raised.value.service == service
    assert raised.value.cause is cause
    assert raised.value.cleanup_errors == ()
    assert not manager.has_forwards


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
        f"raise SystemExit({SAMPLE_FORWARD_EXIT_CODE})"
    )
    process = SshCommand((sys.executable, "-c", code)).popen("host", "ignored")
    try:
        assert process.wait(timeout=5) == SAMPLE_FORWARD_EXIT_CODE
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
        assert command.calls[0][2] == SshLocalForward(first_port, "127.64.0.1", 6333)
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


def test_forward_manager_rejects_a_service_already_forwarded(monkeypatch):
    service = Service("gdb", 32177, 3333)
    command = _ForwardCommand(_ForwardProcess(None))
    manager = _ForwardManager(command, "host")
    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    monkeypatch.setattr(_ForwardManager, "_await_ready", staticmethod(lambda *_args: True))

    try:
        manager.start((service,), "127.64.0.1")
        with pytest.raises(SessionError):
            manager.start((service,), "127.64.0.1")
        assert len(command.calls) == 1
    finally:
        manager.close()


def test_failed_forward_batch_rolls_back_all_processes_and_allows_retry(monkeypatch):
    services = (Service("gdb", 32188, 3333), Service("tcl", 32199, 6333))
    failed_processes = (_ForwardProcess(None), _ForwardProcess(None))
    retry_processes = (_ForwardProcess(None), _ForwardProcess(None))
    command = _ForwardCommand(*failed_processes, *retry_processes)
    manager = _ForwardManager(command, "host")
    readiness = iter((True, False, True, True))
    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    monkeypatch.setattr(
        _ForwardManager,
        "_await_ready",
        staticmethod(lambda *_args: next(readiness)),
    )

    with pytest.raises(SessionError):
        manager.start(services, "127.64.0.1")

    assert [process.terminate_calls for process in failed_processes] == [1, 1]
    assert [process.close_stderr_calls for process in failed_processes] == [1, 1]
    has_forwards_after_failure = manager.has_forwards
    assert not has_forwards_after_failure

    manager.start(services, "127.64.0.1")
    assert manager.has_forwards
    manager.close()
    assert [process.terminate_calls for process in failed_processes] == [1, 1]
    assert [process.terminate_calls for process in retry_processes] == [1, 1]
    assert [process.close_stderr_calls for process in failed_processes] == [1, 1]
    assert [process.close_stderr_calls for process in retry_processes] == [1, 1]


def test_failed_forward_batch_keeps_startup_error_and_cleanup_diagnostics(monkeypatch):
    services = (Service("gdb", 32210, 3333), Service("tcl", 32221, 6333))
    first_cleanup_error = RuntimeError("first rollback failed")
    first_stream_error = RuntimeError("first stream cleanup failed")
    second_cleanup_error = RuntimeError("second rollback failed")
    processes = (
        _ForwardProcess(
            None,
            terminate_error=first_cleanup_error,
            close_stderr_error=first_stream_error,
        ),
        _ForwardProcess(None, terminate_error=second_cleanup_error),
    )
    manager = _ForwardManager(_ForwardCommand(*processes), "host")
    startup_error = KeyboardInterrupt("startup interrupted")
    readiness_calls = 0

    def await_ready(*_args):
        nonlocal readiness_calls
        readiness_calls += 1
        if readiness_calls == 2:
            raise startup_error
        return True

    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    monkeypatch.setattr(_ForwardManager, "_await_ready", staticmethod(await_ready))

    with pytest.raises(KeyboardInterrupt) as raised:
        manager.start(services, "127.64.0.1")

    assert raised.value is startup_error
    assert [process.terminate_calls for process in processes] == [1, 1]
    assert [process.close_stderr_calls for process in processes] == [1, 1]
    notes = raised.value.__notes__
    assert any("first rollback failed" in note for note in notes)
    assert any("first stream cleanup failed" in note for note in notes)
    assert any("second rollback failed" in note for note in notes)
    assert not manager.has_forwards
    manager.close()
    assert [process.close_stderr_calls for process in processes] == [1, 1]


def test_failed_later_forward_batch_preserves_committed_processes(monkeypatch):
    committed_service = Service("gdb", 32232, 3333)
    failed_services = (Service("tcl", 32243, 6333), Service("telnet", 32254, 4444))
    committed_process = _ForwardProcess(None)
    failed_processes = (_ForwardProcess(None), _ForwardProcess(None))
    manager = _ForwardManager(
        _ForwardCommand(committed_process, *failed_processes),
        "host",
    )
    readiness = iter((True, True, False))
    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    monkeypatch.setattr(
        _ForwardManager,
        "_await_ready",
        staticmethod(lambda *_args: next(readiness)),
    )

    manager.start((committed_service,), "127.64.0.1")
    with pytest.raises(SessionError):
        manager.start(failed_services, "127.64.0.1")

    assert committed_process.terminate_calls == 0
    assert [process.terminate_calls for process in failed_processes] == [1, 1]
    assert committed_process.close_stderr_calls == 0
    assert [process.close_stderr_calls for process in failed_processes] == [1, 1]
    assert manager.has_forwards

    manager.close()
    assert committed_process.terminate_calls == 1
    assert [process.terminate_calls for process in failed_processes] == [1, 1]
    assert committed_process.close_stderr_calls == 1
    assert [process.close_stderr_calls for process in failed_processes] == [1, 1]


@pytest.mark.parametrize("phase", ("launch-handoff", "commit-handoff", "readiness"))
def test_sigint_during_forward_start_preserves_cleanup_ownership(monkeypatch, phase):
    service = Service("gdb", 32265, 3333)
    popen = subprocess.Popen
    read = os.read
    sigmask = signal.pthread_sigmask
    managed_processes: list[ManagedSshProcess] = []
    previous_handler = signal.getsignal(signal.SIGINT)
    previous_mask = sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    expected_mask = sigmask(signal.SIG_BLOCK, set())
    requested = False
    services_at_delivery: tuple[Service, ...] = ()

    def request_interrupt():
        nonlocal requested
        requested = True
        signal.raise_signal(signal.SIGINT)

    class InterruptingCommand(SshCommand):
        @override
        def popen(
            self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
        ) -> ManagedSshProcess:
            process = super().popen(host, remote_command, local_forward=local_forward)
            managed_processes.append(process)
            if phase == "launch-handoff":
                request_interrupt()
            return process

    manager = _ForwardManager(InterruptingCommand(), "host")

    def launch(argv, **kwargs):
        # Substitute the executable only; production transport management,
        # readiness observation, and cleanup still operate on a real child.
        code = shlex.split(argv[-1])[2]
        return popen([sys.executable, "-u", "-c", code], **kwargs)

    def read_readiness(descriptor, size):
        if phase == "readiness" and any(
            process.stdout is not None and descriptor == process.stdout.fileno()
            for process in managed_processes
        ):
            request_interrupt()
        return read(descriptor, size)

    def restore_signal_mask(how, mask):
        # Interrupt at the effectful boundary that exposes committed ownership.
        if phase == "commit-handoff" and how == signal.SIG_SETMASK and manager.has_forwards:
            request_interrupt()
        return sigmask(how, mask)

    def handle_interrupt(_signum, _frame):
        nonlocal services_at_delivery
        services_at_delivery = manager.services
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "read", read_readiness)
    monkeypatch.setattr(signal, "pthread_sigmask", restore_signal_mask)
    monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))
    signal.signal(signal.SIGINT, handle_interrupt)
    try:
        with pytest.raises(KeyboardInterrupt):
            manager.start((service,), "127.64.0.1")
        assert requested
        assert sigmask(signal.SIG_BLOCK, set()) == expected_mask
        if phase == "commit-handoff":
            assert services_at_delivery == (service,)
            assert manager.check_health() == ()
        else:
            assert services_at_delivery == ()
        manager.close()
        assert managed_processes
        for process in managed_processes:
            assert process.poll() is not None
            assert process.stdin is not None and process.stdin.closed
            assert process.stdout is not None and process.stdout.closed
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        sigmask(signal.SIG_SETMASK, previous_mask)
        manager.close()
        for process in managed_processes:
            _stop_process(process)


class _ForwardProcess:
    def __init__(
        self,
        returncode,
        *,
        terminate_error: BaseException | None = None,
        close_stderr_error: BaseException | None = None,
    ):
        self.stdin = io.BytesIO()
        self.stdout = None
        self.stderr = None
        self.returncode = returncode
        self.args = ("fake-forward",)
        self.terminate_error = terminate_error
        self.close_stderr_error = close_stderr_error
        self.terminate_calls = 0
        self.close_stderr_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        self.returncode = 0

    def kill(self):
        self.returncode = -signal.SIGKILL

    def stderr_tail(self):
        return b""

    def close_stderr(self):
        self.close_stderr_calls += 1
        if self.close_stderr_error is not None:
            raise self.close_stderr_error


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
