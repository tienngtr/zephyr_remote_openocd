# SPDX-License-Identifier: Apache-2.0

"""Local ``--log-file`` ownership and result projection (SAD §39).

The real ``SessionController``, ``HelperConnection``, authority, and local log
sink run against a pipe-backed helper peer. Only the SSH transport and helper
deployment are replaced, and the sink's ``os.write`` boundary where a test
controls short writes, failures, or blocking.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO, override

import pytest
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    ProcessAcquiredHandler,
    SshCommand,
    SshForwardSpec,
)
from zephyr_remote_openocd.controller.authority import SessionError
from zephyr_remote_openocd.controller.cancellation import OperationCancelled
from zephyr_remote_openocd.controller.helper_connection import HelperConnection
from zephyr_remote_openocd.controller.plan import SessionPlan
from zephyr_remote_openocd.controller.session import SessionController
from zephyr_remote_openocd.protocol.arguments import log_spool_template
from zephyr_remote_openocd.protocol.outcome import (
    ChildResult,
    CleanupReport,
    CompletionPolicy,
    Diagnostic,
    LogReport,
    Outcome,
    TerminalSnapshot,
    Trigger,
)
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import encode_frame, encode_log_data, terminal_fields

from tests.control_support import HelperPeerProcess
from tests.process_support import cleanup_on_exit

CHUNKS = (bytes(range(256)), b"\r\n\x00 second chunk\xff", b"third")
DATA = b"".join(CHUNKS)


class PeerCommand(SshCommand):
    """Deliver the peer as the helper control transport; staging is empty."""

    peer: HelperPeerProcess | None
    popens: list[str]

    def __init__(self, peer: HelperPeerProcess | None) -> None:
        super().__init__(("configured-ssh",))
        object.__setattr__(self, "peer", peer)
        object.__setattr__(self, "popens", [])

    @override
    def popen(
        self,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired: ProcessAcquiredHandler | None = None,
    ) -> ManagedSshProcess:
        del host, local_forward
        self.popens.append(remote_command)
        assert self.peer is not None, "no remote session may be acquired"
        acquired: Any = self.peer
        if on_acquired is not None:
            on_acquired(acquired)
        return acquired

    @override
    def run_stream(
        self, host: str, remote_command: str, input_stream: BinaryIO, *, timeout: float = 60
    ) -> subprocess.CompletedProcess[bytes]:
        del host, remote_command, input_stream, timeout
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


@pytest.fixture(autouse=True)
def deployed(monkeypatch: pytest.MonkeyPatch) -> None:
    deployment = DeploymentResult("/helper.py", "digest", False)
    monkeypatch.setattr(session_module, "deploy_helper", lambda *_args: deployment)


def _process(policy: CompletionPolicy) -> ProcessSpec:
    template = log_spool_template()
    return ProcessSpec(
        ("openocd", "--log_output", template.preview()),
        literal_prefix=1,
        argv_templates=((2, template),),
        completion_policy=policy,
    )


def _log_events(chunks: tuple[bytes, ...] = CHUNKS) -> tuple[bytes, ...]:
    offsets = [sum(len(chunk) for chunk in chunks[:index]) for index in range(len(chunks))]
    return tuple(
        encode_log_data(offset, chunk) for offset, chunk in zip(offsets, chunks, strict=True)
    )


def _terminal(
    log: LogReport,
    *,
    policy: CompletionPolicy = CompletionPolicy.PROCESS_EXIT,
    failure: Diagnostic | None = None,
) -> bytes:
    if failure is not None:
        outcome = Outcome(Trigger.HELPER_FAILURE, failure, child_result=ChildResult(-15, True))
    elif policy == CompletionPolicy.PROCESS_EXIT:
        outcome = Outcome(Trigger.CHILD_EXIT, child_result=ChildResult(0, False))
    else:
        outcome = Outcome(Trigger.CONTROLLER_EOF, child_result=ChildResult(-15, True))
    snapshot = TerminalSnapshot(outcome, CleanupReport("confirmed", "confirmed"), log)
    return encode_frame("SESSION_ENDED", **terminal_fields(snapshot))


def _session(
    target: Path,
    events: tuple[bytes, ...],
    *,
    policy: CompletionPolicy = CompletionPolicy.PROCESS_EXIT,
    later: tuple[bytes, ...] | None = None,
) -> tuple[SessionController, HelperPeerProcess]:
    peer = HelperPeerProcess(
        (encode_frame("ATTEMPT", argv=["openocd", "--log_output", '"/w/log"']), *events),
        later=later,
    )
    plan = SessionPlan("host", PeerCommand(peer), _process(policy), log_file=str(target))
    return SessionController.prepare(plan), peer


type SinkWrite = Callable[[int, bytes], int]


def _await_result(session: SessionController) -> None:
    """Wait for the one-shot result, as flash does before closure."""
    with suppress(SessionError):
        session.wait_for_openocd_exit()


@pytest.fixture
def sink_writes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[SinkWrite]]:
    """Script ``os.write`` on regular-file sinks; other descriptors are unaffected."""
    write = os.write
    scripted: list[SinkWrite] = []

    def controlled(descriptor: int, data: Any) -> int:
        if scripted and stat.S_ISREG(os.fstat(descriptor).st_mode):
            return scripted.pop(0)(descriptor, bytes(data))
        return write(descriptor, data)

    monkeypatch.setattr(os, "write", controlled)
    yield scripted


def _short_write(size: int) -> SinkWrite:
    def write(descriptor: int, data: bytes) -> int:
        written = os.pwrite(descriptor, data[:size], os.lseek(descriptor, 0, os.SEEK_CUR))
        os.lseek(descriptor, written, os.SEEK_CUR)
        return written

    return write


def _failure(error: OSError) -> SinkWrite:
    def write(_descriptor: int, _data: bytes) -> int:
        raise error

    return write


@pytest.mark.timeout(30)
def test_complete_relay_writes_the_exact_bytes_over_truncated_content(tmp_path: Path) -> None:
    target = tmp_path / "openocd.log"
    target.write_bytes(b"stale content that is longer than nothing" * 100)
    session, peer = _session(target, (*_log_events(), _terminal(LogReport(len(DATA), True))))
    with cleanup_on_exit(session.close):
        session.acquire()
        _await_result(session)
    session.finish()
    assert target.read_bytes() == DATA
    # The local pathname is used locally only; it never reaches the helper.
    assert str(target).encode() not in peer.written


@pytest.mark.timeout(30)
def test_short_write_is_completed_with_the_remaining_bytes(
    tmp_path: Path, sink_writes: list[SinkWrite]
) -> None:
    target = tmp_path / "openocd.log"
    sink_writes.extend((_short_write(7), _short_write(1)))
    session, _peer = _session(target, (*_log_events(), _terminal(LogReport(len(DATA), True))))
    with cleanup_on_exit(session.close):
        session.acquire()
        _await_result(session)
    session.finish()
    assert target.read_bytes() == DATA


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "failure",
    ("short-then-error", "short-then-no-progress", "first-error", "later-chunk-error"),
)
def test_local_write_failure_keeps_an_exact_prefix_and_fails_a_complete_relay(
    tmp_path: Path, sink_writes: list[SinkWrite], failure: str
) -> None:
    target = tmp_path / "openocd.log"
    no_space = OSError(errno.ENOSPC, "No space left on device")
    scripts: dict[str, tuple[list[SinkWrite], bytes]] = {
        "short-then-error": ([_short_write(10), _failure(no_space)], CHUNKS[0][:10]),
        "short-then-no-progress": ([_short_write(10), lambda _d, _b: 0], CHUNKS[0][:10]),
        "first-error": ([_failure(no_space)], b""),
        "later-chunk-error": (
            [_short_write(len(CHUNKS[0])), _failure(no_space)],
            CHUNKS[0],
        ),
    }
    script, prefix = scripts[failure]
    # Once the scripted calls are used up, any further sink write is a defect.
    sink_writes.extend((*script, _failure(OSError("written after a local write failure"))))
    remaining = len(sink_writes)
    session, _peer = _session(target, (*_log_events(), _terminal(LogReport(len(DATA), True))))
    with cleanup_on_exit(session.close):
        session.acquire()
        _await_result(session)
    with pytest.raises(SessionError, match="cannot write OpenOCD log file") as raised:
        session.finish()
    assert "written after" not in str(raised.value)
    assert target.read_bytes() == prefix
    assert len(sink_writes) == remaining - len(script)


@pytest.mark.timeout(30)
def test_incomplete_relay_fails_an_otherwise_successful_flash(tmp_path: Path) -> None:
    target = tmp_path / "openocd.log"
    incomplete = LogReport(
        len(CHUNKS[0]), False, Diagnostic("LOG_RELAY_OVERFLOW", "log relay buffer overflowed")
    )
    session, _peer = _session(target, (*_log_events(CHUNKS[:1]), _terminal(incomplete)))
    with cleanup_on_exit(session.close):
        session.acquire()
        _await_result(session)
    assert session.poll_operation() == 0
    with pytest.raises(SessionError, match="LOG_RELAY_OVERFLOW"):
        session.finish()
    assert target.read_bytes() == CHUNKS[0]


@pytest.mark.timeout(30)
def test_log_failure_leaves_dependent_work_admitted_and_fails_live_operation(
    tmp_path: Path, sink_writes: list[SinkWrite]
) -> None:
    target = tmp_path / "openocd.log"
    sink_writes.append(_failure(OSError(errno.EIO, "I/O error")))
    session, peer = _session(
        target,
        (*_log_events(), encode_frame("READY", remote_address="127.64.0.1", child_pid=1)),
        policy=CompletionPolicy.LIVE_SERVER,
        later=(_terminal(LogReport(len(DATA), True), policy=CompletionPolicy.LIVE_SERVER),),
    )
    with cleanup_on_exit(session.close):
        session.acquire()
        # The write failure was recorded before READY; it closed nothing.
        assert session.poll_operation() is None
        assert session.run_local_client(lambda: "client ran") == "client ran"
        peer.release_later()
    with pytest.raises(SessionError, match="cannot write OpenOCD log file"):
        session.finish()
    assert target.read_bytes() == b""


@pytest.mark.timeout(30)
@pytest.mark.parametrize("established", ("operation", "cancellation", "terminal"))
def test_log_failure_is_a_diagnostic_behind_an_established_failure(
    tmp_path: Path, established: str
) -> None:
    target = tmp_path / "openocd.log"
    incomplete = LogReport(0, False, Diagnostic("LOG_DRAIN_DEADLINE", "final drain expired"))
    failure = Diagnostic("HELPER_FAILURE", "remote failure") if established == "terminal" else None
    session, _peer = _session(target, (_terminal(incomplete, failure=failure),))
    operation_failure: BaseException = (
        OperationCancelled() if established == "cancellation" else RuntimeError("client failed")
    )
    # finish() owns closure and publishes the established failure.
    session.acquire()
    _await_result(session)
    if established != "terminal":
        session.record_operation_failure(operation_failure)
    with pytest.raises(BaseException) as raised:
        session.finish()
    if established != "terminal":
        assert raised.value is operation_failure
    else:
        assert "remote failure" in str(raised.value)
    assert any(
        "OpenOCD log file also failed" in note and "LOG_DRAIN_DEADLINE" in note
        for note in getattr(raised.value, "__notes__", ())
    )


@pytest.mark.timeout(30)
def test_sink_blocked_in_a_reader_write_stays_open_while_the_reader_is_unsettled(
    tmp_path: Path, sink_writes: list[SinkWrite]
) -> None:
    target = tmp_path / "openocd.log"
    entered = threading.Event()
    release = threading.Event()

    def blocked(descriptor: int, data: bytes) -> int:
        entered.set()
        assert release.wait(30)
        return os.pwrite(descriptor, data, 0)

    sink_writes.append(blocked)
    session, _peer = _session(target, (*_log_events(CHUNKS[:1]), _terminal(LogReport(256, True))))
    # finish() below owns closure; the blocked reader makes it report failure.
    session.acquire()
    assert entered.wait(10)
    sink = session._log_file
    assert sink is not None and sink.descriptor is not None
    descriptor = sink.descriptor
    try:
        with pytest.raises(SessionError, match="helper event reader did not stop"):
            session.finish()
        # The unsettled reader may still write, so its descriptor stays open.
        assert stat.S_ISREG(os.fstat(descriptor).st_mode)
    finally:
        release.set()
        connection = session._helper_connection
        assert isinstance(connection, HelperConnection)
        reader = connection._reader_thread
        assert reader is not None
        reader.join(10)
        assert not reader.is_alive()
        os.close(descriptor)
    assert target.read_bytes() == CHUNKS[0]


def _fifo(path: Path) -> None:
    os.mkfifo(path, 0o600)


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    "target_kind", ("fifo-without-reader", "fifo-with-reader", "character-device", "terminal")
)
def test_non_regular_target_is_rejected_unmodified_before_a_remote_session(
    tmp_path: Path, target_kind: str
) -> None:
    descriptors: list[int] = []
    if target_kind.startswith("fifo"):
        target = tmp_path / "log.fifo"
        _fifo(target)
        if target_kind == "fifo-with-reader":
            descriptors.append(os.open(target, os.O_RDONLY | os.O_NONBLOCK))
    elif target_kind == "character-device":
        target = Path(os.devnull)
    else:
        primary, secondary = os.openpty()
        descriptors.extend((primary, secondary))
        os.set_blocking(primary, False)
        target = Path(os.ttyname(secondary))
    command = PeerCommand(None)
    session = SessionController.prepare(
        SessionPlan("host", command, _process(CompletionPolicy.PROCESS_EXIT), log_file=str(target))
    )
    try:
        with cleanup_on_exit(session.close), pytest.raises(SessionError):
            session.acquire()
        assert not command.popens
        if target_kind == "fifo-with-reader":
            assert os.read(descriptors[0], 1) == b""
        elif target_kind == "terminal":
            with pytest.raises(BlockingIOError):
                os.read(descriptors[0], 1)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    with pytest.raises(SessionError):
        session.finish()


@pytest.mark.timeout(30)
@pytest.mark.parametrize("failure", ("open", "truncate"))
def test_open_or_truncation_failure_ends_the_operation_before_a_remote_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    target = tmp_path / ("missing/openocd.log" if failure == "open" else "openocd.log")
    if failure == "truncate":
        target.write_bytes(b"retained")

        def fail_truncate(_descriptor: int, _length: int) -> None:
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(os, "ftruncate", fail_truncate)
    command = PeerCommand(None)
    session = SessionController.prepare(
        SessionPlan("host", command, _process(CompletionPolicy.PROCESS_EXIT), log_file=str(target))
    )
    with cleanup_on_exit(session.close), pytest.raises(SessionError, match="OpenOCD log file"):
        session.acquire()
    assert not command.popens
    if failure == "truncate":
        assert target.read_bytes() == b"retained"


@pytest.mark.timeout(30)
def test_tcl_special_pathname_is_opened_literally(tmp_path: Path) -> None:
    target = tmp_path / 'log [$x] {y} "z" \\w;.log'
    session, peer = _session(target, (*_log_events(), _terminal(LogReport(len(DATA), True))))
    with cleanup_on_exit(session.close):
        session.acquire()
        _await_result(session)
    session.finish()
    assert target.read_bytes() == DATA
    assert [path.name for path in tmp_path.iterdir()] == [target.name]
    assert target.name.encode() not in peer.written
