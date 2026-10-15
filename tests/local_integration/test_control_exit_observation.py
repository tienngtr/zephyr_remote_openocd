# SPDX-License-Identifier: Apache-2.0

"""Control exit is observable while an inherited stdout descriptor stays open."""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, cast, override

import pytest
from zephyr_remote_openocd.controller import helper_connection as connection_module
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._ssh import ManagedSshProcess, SshCommand, SshForwardSpec
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionController,
    SessionError,
    SessionPlan,
)
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.waiting import (
    Descriptor,
    ProcessCompletion,
    WaitResult,
    WaitSources,
    wait_for,
)
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy, Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import MAX_FRAME_SIZE, encode_frame

from tests.control_support import CREATED
from tests.protocol_support import terminal_message

pytestmark = [pytest.mark.local, pytest.mark.timeout(60)]
_NO_WAIT_SOURCES = WaitSources()

PEER = r'''
import hashlib, json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
if " stage " in sys.argv[-1]:
    sys.stdin.buffer.read()
    print(json.dumps(dict(version=1, type="STAGED", files=[], directories=[],
                         byte_count=0, sha256=hashlib.sha256(b"").hexdigest())), flush=True)
    sys.exit(0)
if os.fork() == 0:
    os.close(0)
    os.close(2)
    with (root / "hold").open("rb", buffering=0) as hold:
        with (root / "ack").open("wb", buffering=0) as ack:
            ack.write(b"H")
            while hold.read(1) == b"w":
                try:
                    os.write(1, (root / "noise").read_bytes())
                except BrokenPipeError:
                    pass
                ack.write(b"W")
            os.close(1)
            ack.write(b"D")
    os._exit(0)
os.write(1, (root / "initial").read_bytes())
if (root / "after_start").exists():
    assert json.loads(sys.stdin.buffer.readline())["type"] == "START"
    os.write(1, (root / "final").read_bytes())
if (root / "exit_gate").exists():
    with (root / "exit_gate").open("rb", buffering=0) as gate:
        assert gate.read(1) == b"x"
os._exit(int((root / "status").read_text()))
'''
ATTEMPT = encode_frame("ATTEMPT", argv=["child"])
READY = encode_frame("READY", remote_address="127.64.0.1", child_pid=1)


class ExitPeer:
    """Configure a real transport and deterministically hold its stdout open."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, hold: int, ack: int) -> None:
        self.root = root
        self.transport: ManagedSshProcess | None = None
        self.forward_entries: list[SshForwardSpec] = []
        self.final_read_bytes = 0
        self._noisy = False
        self._raw_read = os.read
        self._hold = hold
        self._ack = ack
        self._exit_observed = threading.Event()
        self.pending_wait = threading.Event()
        self.session: SessionController | None = None
        monkeypatch.setattr(connection_module, "wait_for", self._wait)
        monkeypatch.setattr(connection_module.os, "read", self._read)

    def create(
        self,
        initial: bytes,
        final: bytes | None = None,
        *,
        status: int = 0,
        noisy: bool = False,
        policy: CompletionPolicy = CompletionPolicy.LIVE_SERVER,
        cancellation_handler: Callable[[], None] | None = None,
    ) -> SessionController:
        self._noisy = noisy
        (self.root / "initial").write_bytes(initial)
        (self.root / "final").write_bytes(final or b"")
        (self.root / "status").write_text(str(status))
        if final is not None:
            (self.root / "after_start").touch()
        noise = encode_frame("CHILD_OUTPUT", stream="stdout", payload="x" * 3000, line_end=False)
        (self.root / "noise").write_bytes(noise)
        peer = self.root / "peer.py"
        peer.write_text(PEER)
        owner = self

        class Command(SshCommand):
            @override
            def run_stream(
                self, host: str, remote_command: str, stream: BinaryIO, *, timeout: float = 60
            ):
                assert (owner.root / "after_start").exists(), "dead transport authorized staging"
                return super().run_stream(host, remote_command, stream, timeout=timeout)

            @override
            def popen(
                self,
                host: str,
                remote_command: str,
                *,
                local_forward: SshForwardSpec | None = None,
                on_acquired=None,
            ) -> ManagedSshProcess:
                if local_forward is not None:
                    owner.forward_entries.append(local_forward)
                    raise AssertionError("dead control transport authorized forwarding")
                process = super().popen(host, remote_command, on_acquired=on_acquired)
                owner.transport = process
                # The descendant has inherited stdout and entered its hold loop.
                assert owner._raw_read(owner._ack, 1) == b"H"
                return process

        self.session = SessionController(
            SessionPlan(
                "local-test",
                Command((sys.executable, str(peer), str(self.root))),
                ProcessSpec(("child",), completion_policy=policy),
                forwards=(ForwardSpec("gdb", 45678, 3333),),
            ),
            DeploymentResult("/helper.py", "digest", False),
            cancellation_handler=cancellation_handler,
        )
        return self.session

    def _wait(
        self,
        *,
        readable: Iterable[Descriptor] = (),
        writable: Iterable[Descriptor] = (),
        sources: WaitSources = _NO_WAIT_SOURCES,
        deadline: float | None = None,
    ) -> WaitResult:
        assert self.session is not None
        # Hold the reader at the OS wait boundary until control exit is
        # observable. For START scenarios, first let ordinary creation through.
        # All final bytes therefore remain in the real pipe for the sole reader.
        after_start = (self.root / "after_start").exists()
        snapshot = self.session._authority.snapshot()
        if snapshot.control_observation_pending and not sources.processes:
            self.pending_wait.set()
        if sources.processes and (not after_start or snapshot.allocation is not None):
            result = wait_for(sources=sources)
            assert result.exited
            assert all(process.returncode is None for process in sources.processes)
            assert all(
                os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
                for process in sources.processes
            )
            self._exit_observed.set()
            return result
        return wait_for(readable=readable, writable=writable, sources=sources, deadline=deadline)

    def _read(self, descriptor: int, size: int) -> bytes:
        process = self.transport
        if (
            self._noisy
            and process is not None
            and process.stdout is not None
            and descriptor == process.stdout.fileno()
            and threading.current_thread().name == "zro-helper-events"
            and self._exit_observed.is_set()
        ):
            chunk = self._raw_read(descriptor, size)
            self.final_read_bytes += len(chunk)
            assert self.final_read_bytes <= MAX_FRAME_SIZE, "final scan exceeded finite allowance"
            # Replenish before the next raw read, making would-block impossible.
            # This handshake proves the budget even under continuous arrivals.
            os.write(self._hold, b"w")
            assert self._raw_read(self._ack, 1) == b"W"
            return chunk
        return self._raw_read(descriptor, size)

    def settle(self) -> None:
        assert self.session is not None
        failure = self.session._authority.snapshot().failure
        try:
            self.session.close()
        except BaseException as error:
            assert error is failure

    def close(self) -> None:
        try:
            if self.session is not None:
                self.settle()
        finally:
            os.write(self._hold, b"q")
            if self.transport is not None:
                assert self._raw_read(self._ack, 1) == b"D"


@pytest.fixture
def peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ExitPeer]:
    for name in ("hold", "ack"):
        os.mkfifo(tmp_path / name)
    # The fixture owns both descriptors before constructing the transport scope;
    # context exit closes each even when settlement or another close fails.
    with (
        (tmp_path / "hold").open("r+b", buffering=0) as hold,
        (tmp_path / "ack").open("r+b", buffering=0) as ack,
    ):
        controlled = ExitPeer(tmp_path, monkeypatch, hold.fileno(), ack.fileno())
        try:
            yield controlled
        finally:
            controlled.close()


@pytest.mark.parametrize(
    "initial",
    (b"", CREATED, CREATED + terminal_message(Trigger.CONTROLLER_EOF, child_acquired=False)),
    ids=("before-created", "buffered-created", "created-and-orderly-terminal"),
)
def test_control_exit_prevents_session_acquisition(peer: ExitPeer, initial: bytes) -> None:
    session = peer.create(initial)
    with pytest.raises(SessionError) as raised:
        session.acquire()
    snapshot = session._authority.snapshot()
    assert snapshot.control_ended
    assert (snapshot.allocation is not None) == bool(initial)
    assert (snapshot.terminal is not None) == (b"SESSION_ENDED" in initial)
    assert "control transport ended" in str(raised.value)
    assert not isinstance(raised.value.__cause__, TimeoutError)
    assert peer.transport is not None
    # The wait-boundary handshake above verified non-reaping observation.
    peer.settle()
    assert peer.transport.returncode == 0
    assert not peer.forward_entries


@pytest.mark.parametrize(
    "ready",
    (b"", READY, READY + terminal_message(Trigger.CONTROLLER_EOF)),
    ids=("before-ready", "buffered-ready", "ready-and-orderly-terminal"),
)
def test_control_exit_prevents_forwarding_and_client_launch(peer: ExitPeer, ready: bytes) -> None:
    session = peer.create(CREATED, ATTEMPT + ready)
    with pytest.raises(SessionError):
        session.acquire()
    assert (session._authority.snapshot().ready_address is not None) == bool(ready)
    launched: list[bool] = []
    with pytest.raises(SessionError):
        session.run_local_client(lambda: launched.append(True))
    assert not launched
    assert not peer.forward_entries
    assert not session.forwarded_specs
    assert session.descriptor is None


@pytest.mark.parametrize("status", (0, 23))
@pytest.mark.parametrize("earlier_failure", (False, True))
def test_final_terminal_detail_precedes_transport_status(
    peer: ExitPeer, status: int, earlier_failure: bool
) -> None:
    detail = terminal_message(
        Trigger.HELPER_FAILURE, code="DETAIL", message="specific helper failure"
    )
    session = peer.create(CREATED, ATTEMPT + detail, status=status)

    def record_earlier(_argv: tuple[str, ...]) -> None:
        if earlier_failure:
            session.record_operation_failure(ValueError("earlier operation failure"))

    expected = ValueError if earlier_failure else SessionError
    with pytest.raises(expected) as raised:
        session.acquire(process_start_handler=record_earlier)
    peer.settle()
    snapshot = session._authority.snapshot()
    assert snapshot.terminal is not None
    assert snapshot.terminal.outcome.child_result is None
    assert snapshot.failure is raised.value
    rendered = str(raised.value) + "\n" + "\n".join(getattr(raised.value, "__notes__", ()))
    assert "specific helper failure" in rendered
    if earlier_failure:
        assert "earlier operation failure" in str(raised.value)
    else:
        assert "specific helper failure" in str(raised.value)
    assert any(d.code == "CONTROL_PROCESS_FAILURE" for d in snapshot.diagnostics) == bool(status)
    assert peer.transport is not None and peer.transport.returncode == status


@pytest.mark.parametrize("status", (0, 23))
def test_terminal_child_result_does_not_mask_transport_status(peer: ExitPeer, status: int) -> None:
    terminal = terminal_message(Trigger.CHILD_EXIT, returncode=0)
    session = peer.create(
        CREATED, ATTEMPT + terminal, status=status, policy=CompletionPolicy.PROCESS_EXIT
    )
    session.acquire()
    assert isinstance(session._helper_connection, HelperConnection)
    assert session._helper_connection._join_reader(30)
    if status:
        with pytest.raises(SessionError, match="status 23"):
            session.wait_for_openocd_exit()
    else:
        assert session.wait_for_openocd_exit() == 0
    peer.settle()
    assert session.openocd_returncode == 0
    assert peer.transport is not None and peer.transport.returncode == status


def test_partial_final_frame_fails_without_eof(peer: ExitPeer) -> None:
    session = peer.create(CREATED.rstrip(b"\n"))
    with pytest.raises(SessionError) as raised:
        session.acquire()
    assert isinstance(raised.value.__cause__, ValueError)
    assert session._authority.snapshot().allocation is None
    assert any(d.code == "READER_FAILURE" for d in session._authority.snapshot().diagnostics)


def test_continuously_replenished_final_scan_stops_at_its_budget(peer: ExitPeer) -> None:
    session = peer.create(CREATED, ATTEMPT, noisy=True)
    with pytest.raises(SessionError, match="byte budget"):
        session.acquire()
    assert peer.final_read_bytes == MAX_FRAME_SIZE
    assert session._authority.snapshot().control_ended
    assert not peer.forward_entries


def test_pending_scan_waits_for_terminal_detail_after_buffered_ready(peer: ExitPeer) -> None:
    output_entered = threading.Event()
    release_output = threading.Event()
    thread_errors: list[BaseException] = []
    detail = terminal_message(
        Trigger.HELPER_FAILURE, code="DETAIL", message="final helper failure", child_acquired=True
    )
    output = encode_frame("CHILD_OUTPUT", stream="stdout", payload="final output", line_end=True)
    session = peer.create(CREATED, ATTEMPT + READY + output + detail)

    def block_output(_stream: str, _payload: str, _line_end: bool) -> None:
        output_entered.set()
        assert release_output.wait(30), "final output was not released"

    def release_after_pending_wait() -> None:
        try:
            assert output_entered.wait(30)
            assert peer.pending_wait.wait(30), "coordinator did not wait for final observation"
            snapshot = session._authority.snapshot()
            assert snapshot.control_observation_pending
            assert snapshot.ready_address is not None
            assert snapshot.failure is None
            assert not session.observation_sources().processes
            assert not peer.forward_entries
        except BaseException as error:
            thread_errors.append(error)
        finally:
            release_output.set()

    observer = threading.Thread(target=release_after_pending_wait)
    observer.start()
    try:
        with pytest.raises(SessionError, match="final helper failure"):
            session.acquire(output_handler=block_output)
    finally:
        release_output.set()
        observer.join(30)
    assert not observer.is_alive()
    assert not thread_errors
    peer.settle()
    assert not session._authority.snapshot().control_observation_pending
    assert session.descriptor is None
    assert not peer.forward_entries


@pytest.mark.parametrize("callback", ("output", "attempt"))
def test_cancellation_starts_settlement_while_final_callback_is_blocked(
    peer: ExitPeer, callback: str
) -> None:
    callback_entered = threading.Event()
    release_callback = threading.Event()
    settlement_started = threading.Event()
    settlement_states: list[tuple[bool, bool, bool]] = []
    thread_errors: list[BaseException] = []
    detail = terminal_message(
        Trigger.HELPER_FAILURE, code="DETAIL", message="later helper failure", child_acquired=True
    )
    output = encode_frame("CHILD_OUTPUT", stream="stdout", payload="final output", line_end=True)

    def observe_settlement() -> None:
        snapshot = session._authority.snapshot()
        settlement_states.append(
            (
                snapshot.control_observation_pending,
                isinstance(snapshot.failure, OperationCancelled),
                not release_callback.is_set(),
            )
        )
        settlement_started.set()

    session = peer.create(
        CREATED, ATTEMPT + output + READY + detail, cancellation_handler=observe_settlement
    )

    def block_callback() -> None:
        callback_entered.set()
        assert release_callback.wait(30), "final callback was not released"

    def handle_output(_stream: str, _payload: str, _line_end: bool) -> None:
        if callback == "output":
            block_callback()

    def handle_attempt(_argv: tuple[str, ...]) -> None:
        if callback == "attempt":
            block_callback()

    def interrupt_and_observe_settlement() -> None:
        try:
            assert callback_entered.wait(30)
            os.kill(os.getpid(), signal.SIGINT)
            assert settlement_started.wait(30), "blocked callback prevented settlement entry"
        except BaseException as error:
            thread_errors.append(error)
        finally:
            release_callback.set()

    sender = threading.Thread(target=interrupt_and_observe_settlement)
    sender.start()
    try:
        with pytest.raises(OperationCancelled) as raised, operation_scope():
            try:
                session.acquire(output_handler=handle_output, process_start_handler=handle_attempt)
            finally:
                session.close()
    finally:
        release_callback.set()
        sender.join(30)
    assert not sender.is_alive()
    assert not thread_errors
    assert settlement_states == [(True, True, True)]
    assert session._authority.snapshot().failure is raised.value
    assert any("later helper failure" in note for note in raised.value.__notes__)
    assert session.closed
    assert peer.transport is not None and peer.transport.returncode == 0
    assert not peer.forward_entries


def test_foreground_wait_uses_reader_notification_when_ordinary_output_blocks(
    peer: ExitPeer, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_entered = threading.Event()
    coordinator_waiting = threading.Event()
    release_output = threading.Event()
    thread_errors: list[BaseException] = []
    output = encode_frame("CHILD_OUTPUT", stream="stdout", payload="ordinary output", line_end=True)
    terminal = terminal_message(Trigger.CHILD_EXIT, returncode=0)
    session = peer.create(
        CREATED, ATTEMPT + output + terminal, policy=CompletionPolicy.PROCESS_EXIT
    )
    # This interleaving needs normal reader observation before process exit,
    # rather than the fixture's wait that retains bytes for the final scan.
    monkeypatch.setattr(connection_module, "wait_for", wait_for)
    os.mkfifo(peer.root / "exit_gate")

    def block_output(_stream: str, _payload: str, _line_end: bool) -> None:
        output_entered.set()
        assert release_output.wait(30), "ordinary output was not released"

    def release_after_coordinator_wait() -> None:
        try:
            assert coordinator_waiting.wait(30), "coordinator did not enter its foreground wait"
        except BaseException as error:
            thread_errors.append(error)
        finally:
            release_output.set()

    with (peer.root / "exit_gate").open("r+b", buffering=0) as exit_gate:

        def observe_wait(*, sources: WaitSources, deadline: float | None = None) -> WaitResult:
            assert peer.transport is not None
            if not coordinator_waiting.is_set():
                assert output_entered.wait(30)
                # Exit happens only after the ordinary callback blocks. Observe
                # it without reaping while the reader cannot begin its scan.
                exit_gate.write(b"x")
                assert wait_for(sources=WaitSources(processes=(peer.transport,))).exited
                assert peer.transport.returncode is None
                assert not session._authority.snapshot().control_ended
                # Remove creation hints while the reader is still blocked.
                for notifier in sources.notifiers:
                    notifier.drain()
            assert peer.transport not in sources.processes
            coordinator_waiting.set()
            result = wait_for(sources=sources, deadline=deadline)
            assert not result.exited
            assert result.notified
            return result

        monkeypatch.setattr(session_module, "wait_for", observe_wait)
        observer = threading.Thread(target=release_after_coordinator_wait)
        observer.start()
        try:
            session.acquire(output_handler=block_output)
            assert session.wait_for_openocd_exit() == 0
        finally:
            release_output.set()
            coordinator_waiting.set()
            observer.join(30)
            peer.settle()
    assert not observer.is_alive()
    assert not thread_errors
    assert peer.transport is not None and peer.transport.returncode == 0


@pytest.mark.parametrize("pidfd_available", (True, False), ids=("pidfd", "waitid-fallback"))
def test_control_exit_survives_reaping_during_wait_registration(
    peer: ExitPeer, monkeypatch, pidfd_available
):
    snapshot_taken = threading.Event()
    reaped = threading.Event()
    terminal = terminal_message(Trigger.CONTROLLER_EOF, child_acquired=False)
    session = peer.create(CREATED + terminal)
    returncode = cast(property, inspect.getattr_static(ManagedSshProcess, "returncode")).fget
    assert returncode is not None
    open_pidfd = os.pidfd_open
    waitid = os.waitid
    completion_descriptors: list[int] = []

    def snapshot_returncode(process):
        status = returncode(process)
        if (
            process is peer.transport
            and threading.current_thread() is not threading.main_thread()
            and not snapshot_taken.is_set()
        ):
            assert status is None
            snapshot_taken.set()
            assert reaped.wait(30), "coordinator did not reap the control process"
        return status

    with subprocess.Popen(
        (sys.executable, "-c", "import sys; sys.stdin.read()"), stdin=subprocess.PIPE
    ) as replacement:

        def reused_pidfd(pid, flags=0):
            if not pidfd_available:
                raise OSError("pidfd unavailable")
            if peer.transport is not None and pid == peer.transport.pid and reaped.is_set():
                # Model kernel PID reuse with a real, still-live replacement.
                # The owner reaps after the reader's status snapshot and before
                # completion registration; inherited stdout remains open.
                return open_pidfd(replacement.pid, flags)
            return open_pidfd(pid, flags)

        def reused_waitid(idtype, pid, options):
            if peer.transport is not None and pid == peer.transport.pid and reaped.is_set():
                return waitid(idtype, replacement.pid, options)
            return waitid(idtype, pid, options)

        def observe_wait(
            *, readable=(), writable=(), sources=_NO_WAIT_SOURCES, deadline=None
        ) -> WaitResult:
            # Track only the project's owned completion descriptor, not the
            # temporary pidfds that subprocess.wait() also opens on Python 3.15+.
            for process in sources.processes:
                if (
                    isinstance(process, ProcessCompletion)
                    and process.descriptor is not None
                    and process.descriptor not in completion_descriptors
                ):
                    completion_descriptors.append(process.descriptor)
            if not sources.processes and not reaped.is_set():
                assert snapshot_taken.wait(30), "reader did not snapshot control status"
                assert peer.transport is not None
                assert peer.transport.wait(timeout=30) == 0
                reaped.set()
            result = wait_for(
                readable=readable, writable=writable, sources=sources, deadline=deadline
            )
            if sources.processes and reaped.is_set():
                assert result.exited, "completion registration lost the reaped control process"
                for descriptor in completion_descriptors:
                    os.fstat(descriptor)  # The wait borrows rather than closes this resource.
            return result

        monkeypatch.setattr(ManagedSshProcess, "returncode", property(snapshot_returncode))
        monkeypatch.setattr(os, "pidfd_open", reused_pidfd)
        monkeypatch.setattr(os, "waitid", reused_waitid)
        monkeypatch.setattr(connection_module, "wait_for", observe_wait)
        try:
            with pytest.raises(SessionError):
                session.acquire()
            snapshot = session._authority.snapshot()
            assert snapshot_taken.is_set() and reaped.is_set()
            assert snapshot.control_ended and not snapshot.control_observation_pending
            assert bool(completion_descriptors) is pidfd_available
            assert snapshot.terminal is not None
            assert not any(detail.code == "READER_FAILURE" for detail in snapshot.diagnostics)
            assert not peer.forward_entries
            assert replacement.poll() is None
        finally:
            reaped.set()
            assert replacement.stdin is not None
            replacement.stdin.close()
            replacement.wait(timeout=30)
            peer.settle()
        for descriptor in completion_descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
