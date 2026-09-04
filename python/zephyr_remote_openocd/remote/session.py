# SPDX-License-Identifier: Apache-2.0

"""Generic remote-session lifecycle and backend contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from .model import RemoteSessionRequest, Service, SessionDescriptor, SessionState, StagedFile


class SessionError(RuntimeError):
    pass


class BackendSession(ABC):
    @abstractmethod
    def stage(self, files: Iterable[StagedFile]): ...

    @abstractmethod
    def start(self, services: Iterable[Service]) -> SessionDescriptor: ...

    @abstractmethod
    def forward(self, services: Iterable[Service]) -> None: ...

    @abstractmethod
    def poll(self) -> int | None: ...

    @abstractmethod
    def wait(self, timeout: float | None = None) -> int: ...

    @abstractmethod
    def close(self) -> None: ...


class SessionBackend(ABC):
    @abstractmethod
    def create(self, request: RemoteSessionRequest) -> BackendSession: ...


class RemoteSession:
    def __init__(self, request: RemoteSessionRequest, backend: SessionBackend):
        self.request = request
        self.backend = backend
        self.state = SessionState.NEW
        self.descriptor: SessionDescriptor | None = None
        self.termination_returncode: int | None = None
        self._session: BackendSession | None = None
        self._services = list(request.services)

    def start(self) -> SessionDescriptor:
        if self.state is not SessionState.NEW:
            raise SessionError(f"cannot start session in {self.state.name} state")
        try:
            self._session = self.backend.create(self.request)
            self.state = SessionState.CREATED
            self._session.stage(self.request.staged_files)
            self.state = SessionState.STAGED
            self.state = SessionState.STARTING
            self.descriptor = self._session.start(self.request.services)
            self.state = SessionState.READY
            return self.descriptor
        except BaseException as error:
            self.state = SessionState.FAILED
            if self._session is not None:
                try:
                    self._session.close()
                except BaseException as cleanup_error:
                    # Preserve the startup failure while retaining cleanup
                    # diagnostics for callers and release logs.
                    error.add_note(f"startup failure cleanup also failed: {cleanup_error}")
            raise

    def forward(self, services: Iterable[Service]) -> None:
        if self.state is not SessionState.READY or self._session is None:
            raise SessionError(f"cannot add forwarding in {self.state.name} state")
        additions = tuple(services)
        if not additions:
            return
        combined = (*self._services, *additions)
        names = [item.name for item in combined]
        ports = [item.local_port for item in combined]
        if len(names) != len(set(names)):
            raise SessionError("service names must remain unique")
        if len(ports) != len(set(ports)):
            raise SessionError("local service ports must remain unique")
        try:
            self._session.forward(additions)
            self._services.extend(additions)
        except BaseException:
            self.state = SessionState.FAILED
            self._session.close()
            raise

    def poll(self) -> int | None:
        if self._session is None:
            return self.termination_returncode
        try:
            result = self._session.poll()
        except BaseException as error:
            self.state = SessionState.FAILED
            try:
                self._session.close()
            except BaseException as cleanup_error:
                error.add_note(f"session cleanup failed: {cleanup_error}")
            raise
        if result is not None:
            self.termination_returncode = result
            try:
                self._session.close()
            except BaseException:
                self.state = SessionState.FAILED
                raise
            else:
                if self.state not in {SessionState.STOPPING, SessionState.CLOSED}:
                    self.state = SessionState.CLOSED if result == 0 else SessionState.FAILED
        return result

    def wait(self, timeout: float | None = None) -> int:
        if self._session is None:
            raise SessionError("session has not been started")
        try:
            result = self._session.wait(timeout)
        except BaseException as error:
            self.state = SessionState.FAILED
            try:
                self._session.close()
            except BaseException as cleanup_error:
                error.add_note(f"session cleanup failed: {cleanup_error}")
            raise
        self.termination_returncode = result
        try:
            self._session.close()
        except BaseException:
            self.state = SessionState.FAILED
            raise
        self.state = SessionState.CLOSED if result == 0 else SessionState.FAILED
        return result

    def close(self) -> None:
        if self.state is SessionState.CLOSED:
            return
        self.state = SessionState.STOPPING
        try:
            if self._session is not None:
                self._session.close()
        finally:
            self.state = SessionState.CLOSED

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()
