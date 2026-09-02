"""Production SSH/helper implementation of the session backend."""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import time
from typing import Iterable

from .deploy import DeploymentResult, deploy_helper
from .model import RemoteSessionRequest, Service, SessionAllocation, SessionDescriptor, StagedFile
from .protocol import EventOrder, ProtocolError, read_message, write_message
from .session import BackendSession, SessionBackend, SessionError
from .staging import build_archive


class SshHelperBackend(SessionBackend):
    def __init__(self, *, forward_start_timeout: float = 0.5):
        self.forward_start_timeout = forward_start_timeout

    def create(self, request: RemoteSessionRequest) -> BackendSession:
        deployment = deploy_helper(request.ssh_command, request.host)
        return SshHelperSession(request, deployment, self.forward_start_timeout)


class SshHelperSession(BackendSession):
    def __init__(self, request: RemoteSessionRequest, deployment: DeploymentResult, forward_start_timeout: float):
        self.request = request
        self.deployment = deployment
        self.forward_start_timeout = forward_start_timeout
        self.forwards: list[subprocess.Popen[bytes]] = []
        self.closed = False
        self.events: list[dict[str, object]] = []
        command = f"python3 {shlex.quote(deployment.path)} control"
        self.controller = request.ssh_command.popen(
            request.host, command, "-o", "ControlMaster=no"
        )
        try:
            if self.controller.stdout is None:
                raise SessionError("controller stdout was not captured")
            self._order = EventOrder()
            hello = self._read_event()
            if hello["type"] != "HELLO":
                raise ProtocolError("helper did not begin with HELLO")
            created = self._read_event()
            if created["type"] != "SESSION_CREATED":
                raise ProtocolError("helper did not create a session")
            self.allocation = SessionAllocation(created["session_id"], created["remote_workspace"])
        except BaseException:
            self._stop_process(self.controller)
            raise

    def _read_event(self) -> dict:
        try:
            message = read_message(self.controller.stdout)
        except EOFError as error:
            diagnostic = b""
            if self.controller.stderr is not None:
                diagnostic = self.controller.stderr.read()
            raise SessionError(
                "remote helper terminated: " + diagnostic.decode("utf-8", "replace")
            ) from error
        self._order.accept(message)
        self.events.append(message)
        if message["type"] == "ERROR":
            raise SessionError(f"remote helper error: {message.get('message', 'unknown error')}")
        return message

    def stage(self, files: Iterable[StagedFile]):
        archive = build_archive(files)
        try:
            command = f"python3 {shlex.quote(self.deployment.path)} stage {shlex.quote(self.allocation.remote_workspace)}"
            result = self.request.ssh_command.run_stream(
                self.request.host, command, archive.stream, timeout=60
            )
        finally:
            archive.stream.close()
        if result.returncode:
            raise SessionError(
                f"remote staging failed ({result.returncode}): "
                + result.stderr.decode("utf-8", "replace").strip()
            )
        try:
            message = json.loads(result.stdout)
            if message.get("version") != 1 or message.get("type") != "STAGED":
                raise ValueError("unexpected staging response")
            if tuple(message.get("files", ())) != archive.files:
                raise ValueError("remote staged-file confirmation differs from manifest")
            return message
        except (ValueError, json.JSONDecodeError) as error:
            raise SessionError(f"invalid remote staging response: {result.stdout!r}") from error

    @staticmethod
    def _preflight(port: int) -> str | None:
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port))
            return None
        except OSError as error:
            return f"local port 127.0.0.1:{port} appears unavailable: {error}"

    def start(self, services: Iterable[Service]) -> SessionDescriptor:
        service_list = tuple(services)
        if not service_list:
            raise SessionError("at least one service is required")
        advisories = [message for item in service_list if (message := self._preflight(item.local_port))]
        if self.controller.stdin is None:
            raise SessionError("controller stdin was not captured")
        write_message(
            self.controller.stdin, "START",
            services=[{"name": item.name, "remote_port": item.remote_port} for item in service_list],
        )
        while True:
            event = self._read_event()
            if event["type"] == "SERVICE_READY":
                address = event["remote_address"]
                break
        try:
            for service in service_list:
                spec = f"127.0.0.1:{service.local_port}:{address}:{service.remote_port}"
                process = self.request.ssh_command.popen(
                    self.request.host, None,
                    "-N", "-o", "ControlMaster=no", "-o", "ExitOnForwardFailure=yes", "-L", spec,
                )
                self.forwards.append(process)
                deadline = time.monotonic() + self.forward_start_timeout
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                if process.poll() is not None:
                    detail = b"" if process.stderr is None else process.stderr.read()
                    prefix = "; ".join(advisories)
                    raise SessionError(
                        (prefix + "; " if prefix else "")
                        + f"SSH forwarding failed ({process.returncode}): {detail.decode('utf-8', 'replace').strip()}"
                    )
            return SessionDescriptor(self.allocation, address)
        except BaseException:
            self._close_forwards()
            raise

    def poll(self) -> int | None:
        return self.controller.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.controller.wait(timeout=timeout)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def _close_forwards(self) -> None:
        while self.forwards:
            self._stop_process(self.forwards.pop())

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._close_forwards()
        if self.controller.poll() is None and self.controller.stdin is not None:
            try:
                write_message(self.controller.stdin, "STOP")
                self.controller.stdin.close()
                self.controller.wait(timeout=5)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                self._stop_process(self.controller)
        self._stop_process(self.controller)
