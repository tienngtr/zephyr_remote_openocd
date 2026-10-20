# SPDX-License-Identifier: Apache-2.0

"""Controlled SSH/helper boundaries for real forwarding-policy tests."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
from collections.abc import Callable, Iterable
from typing import BinaryIO, cast, override
from unittest.mock import Mock, PropertyMock, create_autospec

import pytest
from zephyr_remote_openocd.controller import _forwarding as forwarding_module
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    ProcessAcquiredHandler,
    SshCommand,
    SshForwardSpec,
)
from zephyr_remote_openocd.controller.api import (
    ForwardAdvisory,
    ForwardSpec,
    SessionAllocation,
    SessionController,
    SessionPlan,
)
from zephyr_remote_openocd.controller.authority import SessionAuthority
from zephyr_remote_openocd.controller.helper_connection import OutputHandler, ProcessStartHandler
from zephyr_remote_openocd.controller.waiting import Notifier, WaitResult, WaitSources, wait_for
from zephyr_remote_openocd.protocol.arguments import materialize_argv
from zephyr_remote_openocd.protocol.commands import decode_command, encode_start
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import encode_frame

GDB = ForwardSpec("gdb", 3333, 3333)
TCL = ForwardSpec("tcl", 6333, 6333)
TELNET = ForwardSpec("telnet", 4444, 4444)
RTT = ForwardSpec("rtt", 5555, 5555)


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
        # Completion sources read live state; a never-exiting pid stands in
        # for a running transport, and returncode reports its observed exit.
        type(self.mock).returncode = PropertyMock(side_effect=lambda: self.returncode)
        type(self.mock).pid = PropertyMock(return_value=os.getpid())
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
        self._authority: SessionAuthority | None = None
        self.listeners: tuple[ListenerSpec, ...] = ()
        self.close_calls = 0
        self.on_wait: Callable[[], None] | None = None
        self.process_start_handler: ProcessStartHandler | None = None
        self.preferred_address: str | None = None

    @property
    def authority(self) -> SessionAuthority:
        assert self._authority is not None, "session authority must be bound before use"
        return self._authority

    @authority.setter
    def authority(self, authority: SessionAuthority) -> None:
        self._authority = authority

    def acquire(self) -> None:
        pass

    def start_process(
        self,
        process: ProcessSpec,
        listeners: Iterable[ListenerSpec],
        *,
        preferred_address: str | None = None,
    ) -> str:
        self.preferred_address = preferred_address
        if self.process_start_handler is not None:
            start = encode_start(process, (), preferred_address=preferred_address)
            request = decode_command(start)
            self.process_start_handler(
                materialize_argv(
                    process.argv,
                    workspace=self.allocation.remote_workspace,
                    address="127.64.0.1",
                    literal_prefix=process.literal_prefix,
                    argv_templates=request.argv_templates,
                )
            )
        self.listeners = tuple(listeners)
        return "127.64.0.1"

    def observe_status(self) -> None:
        pass

    def timeout_expired(self, timeout: float) -> subprocess.TimeoutExpired:
        return subprocess.TimeoutExpired(("helper",), timeout)

    def close(self) -> None:
        self.close_calls += 1


class ControlledSshCommand(SshCommand):
    processes: dict[int, ControlledForward]

    def __init__(self) -> None:
        super().__init__(("configured-ssh",))
        object.__setattr__(self, "processes", {})

    def process(self, forward: ForwardSpec) -> ControlledForward:
        if forward.local_port not in self.processes:
            self.processes[forward.local_port] = ControlledForward()
        return self.processes[forward.local_port]

    @override
    def popen(
        self,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired: ProcessAcquiredHandler | None = None,
    ) -> ManagedSshProcess:
        del host, remote_command
        assert local_forward is not None
        assert local_forward.remote_address == "127.64.0.1"
        process = self.processes[local_forward.local_port]
        if process.creation_error is not None:
            raise process.creation_error
        acquired_process = process.managed
        if on_acquired is not None:
            on_acquired(acquired_process)
        return acquired_process

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
            encode_frame(
                "STAGED",
                files=[],
                directories=[],
                byte_count=0,
                sha256=hashlib.sha256(b"").hexdigest(),
            ),
            b"",
        )

    @override
    def run(
        self,
        host: str,
        remote_command: str,
        *,
        input_data: bytes | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("run() is not expected")


class ForwardingHarness:
    """Wire real session/manager objects to controlled external boundaries."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ssh = ControlledSshCommand()
        self.helper = ControlledHelper()
        self.advisories: list[ForwardAdvisory] = []
        deployment = DeploymentResult("/helper.py", "digest", False)
        monkeypatch.setattr(session_module, "deploy_helper", lambda *_args: deployment)
        monkeypatch.setattr(session_module, "HelperConnection", self.make_helper)
        monkeypatch.setattr(session_module, "wait_for", self.wait)
        monkeypatch.setattr(forwarding_module, "await_ready", self.await_ready)
        monkeypatch.setattr(forwarding_module, "preflight", lambda _service: None)

    def wait(self, *, sources: WaitSources, deadline: float | None = None) -> WaitResult:
        # Publish controlled reader facts at the actual foreground wait boundary;
        # collecting process sources must not perform helper lifecycle work.
        assert self.helper.on_wait is not None, "helper wait needs an explicit state transition"
        self.helper.on_wait()
        return wait_for(sources=sources, deadline=deadline)

    def make_helper(
        self,
        ssh_command: SshCommand,
        host: str,
        deployment: DeploymentResult,
        output_handler: OutputHandler | None = None,
        *,
        process_start_handler: ProcessStartHandler | None = None,
        authority: SessionAuthority,
        notifier: Notifier,
    ) -> ControlledHelper:
        del ssh_command, host, deployment, output_handler, notifier
        self.helper.authority = authority
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
        forwards: tuple[ForwardSpec, ...] = (GDB, TCL, TELNET),
        auxiliary: tuple[ForwardSpec, ...] = (TCL, TELNET),
    ) -> SessionPlan:
        for forward in forwards:
            self.ssh.process(forward)
        return SessionPlan(
            "host",
            self.ssh,
            ProcessSpec(("openocd",)),
            forwards=forwards,
            auxiliary_forwards=auxiliary,
        )

    def prepare(
        self,
        forwards: tuple[ForwardSpec, ...] = (GDB, TCL, TELNET),
        auxiliary: tuple[ForwardSpec, ...] = (TCL, TELNET),
    ) -> SessionController:
        return SessionController.prepare(
            self.request(forwards, auxiliary),
            advisory_handler=self.advisories.append,
        )
