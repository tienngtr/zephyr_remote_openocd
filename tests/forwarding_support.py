# SPDX-License-Identifier: Apache-2.0

"""Controlled SSH/helper boundaries for real forwarding-policy tests."""

from __future__ import annotations

import hashlib
import io
import subprocess
from collections.abc import Callable, Iterable
from typing import BinaryIO, cast, override
from unittest.mock import Mock, create_autospec

import pytest
from zephyr_remote_openocd.remote import backend as backend_module
from zephyr_remote_openocd.remote.backend import RemoteSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.forwarding import ForwardAdvisory, _ForwardManager
from zephyr_remote_openocd.remote.helper_client import _HelperClient, _HelperCloseResult
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
)
from zephyr_remote_openocd.remote.protocol import decode_message, encode_message, write_start
from zephyr_remote_openocd.remote.ssh import ManagedSshProcess, SshCommand, SshLocalForward
from zephyr_remote_openocd.remote_helper import decode_command, materialize_argv

GDB = Service("gdb", 3333, 3333)
TCL = Service("tcl", 6333, 6333)
TELNET = Service("telnet", 4444, 4444)
RTT = Service("rtt", 5555, 5555)


class ControlledForward:
    """Control only the managed SSH subprocess boundary."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.ready = True
        self.readiness_error: BaseException | None = None
        self.creation_error: OSError | None = None
        self.cleanup_error: BaseException | None = None
        self.diagnostic = b"forward diagnostic"
        self.mock: Mock = create_autospec(ManagedSshProcess, instance=True, spec_set=True)
        self.mock.stdin = io.BytesIO()
        self.mock.stdout = None
        self.mock.args = ("configured-ssh",)
        self.mock.poll.side_effect = lambda: self.returncode
        self.mock.wait.side_effect = lambda timeout=None: self.returncode
        self.mock.terminate.side_effect = self.terminate
        self.mock.kill.side_effect = self.terminate
        self.mock.stderr_tail.side_effect = lambda: self.diagnostic
        self.mock.close_stderr.side_effect = self.close_stderr
        # The sole cast is confined to the externally managed subprocess mock.
        self.managed = cast(ManagedSshProcess, self.mock)

    def terminate(self) -> None:
        self.returncode = 0

    def close_stderr(self) -> None:
        if self.cleanup_error is not None:
            raise self.cleanup_error


class ControlledHelper:
    """Helper-control transport facts, with no forwarding policy."""

    allocation = SessionAllocation("session", "/workspace")

    def __init__(self) -> None:
        self.openocd_returncode: int | None = None
        self.services: tuple[Service, ...] = ()
        self.close_calls = 0
        self.on_wait: Callable[[], None] | None = None
        self.process_start_handler: Callable[[tuple[str, ...]], None] | None = None

    def start_process(self, process: RemoteProcess, services: Iterable[Service]) -> str:
        if self.process_start_handler is not None:
            stream = io.BytesIO()
            write_start(stream, process, ())
            request = decode_command(decode_message(stream.getvalue()))
            self.process_start_handler(
                materialize_argv(
                    process.argv,
                    workspace=self.allocation.remote_workspace,
                    address="127.64.0.1",
                    literal_prefix=process.literal_prefix,
                    argv_templates=request.argv_templates,
                )
            )
        self.services = tuple(services)
        return "127.64.0.1"

    def recorded_openocd_exit(self) -> int | None:
        return self.openocd_returncode

    def wait_for_change(self, timeout: float | None) -> None:
        del timeout
        assert self.on_wait is not None, "helper wait must have an explicit state transition"
        self.on_wait()

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(("helper",), timeout)

    def close(self) -> _HelperCloseResult:
        self.close_calls += 1
        return _HelperCloseResult(None, ())


class ControlledSshCommand(SshCommand):
    processes: dict[int, ControlledForward]

    def __init__(self) -> None:
        super().__init__(("configured-ssh",))
        object.__setattr__(self, "processes", {})

    def process(self, service: Service) -> ControlledForward:
        if service.local_port not in self.processes:
            self.processes[service.local_port] = ControlledForward()
        return self.processes[service.local_port]

    @override
    def popen(
        self, host: str, remote_command: str, *, local_forward: SshLocalForward | None = None
    ) -> ManagedSshProcess:
        del host, remote_command
        assert local_forward is not None
        assert local_forward.remote_address == "127.64.0.1"
        process = self.processes[local_forward.local_port]
        if process.creation_error is not None:
            raise process.creation_error
        return process.managed

    @override
    def run_stream(
        self, host: str, remote_command: str, input_stream: BinaryIO, *, timeout: float = 60
    ) -> subprocess.CompletedProcess[bytes]:
        del host, timeout
        assert " stage " in remote_command
        assert input_stream.read()
        return subprocess.CompletedProcess(
            (),
            0,
            encode_message(
                "STAGED",
                files=[],
                directories=[],
                byte_count=0,
                sha256=hashlib.sha256(b"").hexdigest(),
            ),
            b"",
        )

    @override
    def run(self, host: str, remote_command: str, *, input_data=None, timeout=15):
        raise AssertionError("run() is not expected")


class ForwardingHarness:
    """Wire real session/manager objects to controlled external boundaries."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ssh = ControlledSshCommand()
        self.helper = ControlledHelper()
        self.advisories: list[ForwardAdvisory] = []
        deployment = DeploymentResult("/helper.py", "digest", False)
        monkeypatch.setattr(backend_module, "deploy_helper", lambda *_args: deployment)
        monkeypatch.setattr(_HelperClient, "open", self.open_helper)
        monkeypatch.setattr(_ForwardManager, "_await_ready", staticmethod(self.await_ready))
        monkeypatch.setattr(_ForwardManager, "_preflight", staticmethod(lambda _service: None))

    def open_helper(self, *_args, process_start_handler=None, **_kwargs):
        self.helper.process_start_handler = process_start_handler
        return self.helper

    def await_ready(self, managed: ManagedSshProcess, sentinel: str, deadline: float) -> bool:
        del sentinel, deadline
        process = next(item for item in self.ssh.processes.values() if item.managed is managed)
        if process.readiness_error is not None:
            raise process.readiness_error
        return process.ready

    def request(
        self,
        services: tuple[Service, ...] = (GDB, TCL, TELNET),
        auxiliary: tuple[Service, ...] = (TCL, TELNET),
    ) -> RemoteSessionRequest:
        for service in services:
            self.ssh.process(service)
        return RemoteSessionRequest(
            "host",
            self.ssh,
            RemoteProcess(("openocd",)),
            services=services,
            auxiliary_services=auxiliary,
        )

    def open(
        self,
        services: tuple[Service, ...] = (GDB, TCL, TELNET),
        auxiliary: tuple[Service, ...] = (TCL, TELNET),
    ) -> RemoteSession:
        return RemoteSession.open(
            self.request(services, auxiliary),
            advisory_handler=self.advisories.append,
        )
