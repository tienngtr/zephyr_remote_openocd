# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, override

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.controller import helper_connection as connection_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    ProcessAcquiredHandler,
    ProcessControl,
    SshCommand,
    SshForwardSpec,
)
from zephyr_remote_openocd.controller.authority import SessionAuthority, SessionError
from zephyr_remote_openocd.controller.helper_connection import (
    HelperConnection,
    OutputHandler,
    ProcessStartHandler,
)
from zephyr_remote_openocd.controller.paths import PathPlanner
from zephyr_remote_openocd.controller.waiting import Descriptor, WaitResult, WaitSources, wait_for
from zephyr_remote_openocd.openocd.flash import FlashInputs, build_flash_plan
from zephyr_remote_openocd.protocol.arguments import (
    ArgumentTemplate,
    SessionValue,
    TclWord,
    materialize_argv,
    materialize_path,
)
from zephyr_remote_openocd.protocol.commands import decode_command, write_start
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy, Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.wire import ProtocolError, decode_frame, encode_frame

from tests.control_support import CREATED, ControlProcess, HelperPeerProcess
from tests.protocol_support import terminal_message, terminal_snapshot
from tests.session_support import NotifiedAuthority

OPENOCD_FAILURE_RC = 7

# These tests intentionally retain one typing escape at the process-control
# boundary.  HelperConnection, with stop_process() and ProcessCompletion, uses
# every public ManagedSshProcess operation except acquire(): args, pid,
# returncode, stdin, stdout, poll, wait, terminate, kill, stderr_tail, and
# close_stderr.  A production protocol would duplicate that whole concrete API
# solely for tests.  The process-shaped doubles below inject behavior a real
# wrapper cannot express deterministically: poll and wait failures, stdin-close
# and exit races, termination and stderr-close failures, reader shutdown
# ordering, and partial event-stream delivery.  Tests that do not need those
# faults use real ManagedSshProcess construction in the local-integration layer.


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


_NO_WAIT_SOURCES = WaitSources()


def _child_returncode(authority: SessionAuthority) -> int:
    terminal = authority.snapshot().terminal
    assert terminal is not None and terminal.outcome.child_result is not None
    return terminal.outcome.child_result.returncode


def _open_helper_client(
    process: ControlProcess,
    wiring: NotifiedAuthority,
    *,
    process_start_handler: ProcessStartHandler | None = None,
    output_handler: OutputHandler | None = None,
) -> HelperConnection:
    class Command(_PopenOnlySshCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired: ProcessAcquiredHandler | None = None,
        ) -> ManagedSshProcess:
            del host, remote_command, local_forward
            # The single process-control typing escape described above.
            acquired_process: Any = process
            if on_acquired is not None:
                on_acquired(acquired_process)
            return acquired_process

    client = HelperConnection(
        Command(),
        "host",
        DeploymentResult("/helper.py", "digest", False),
        output_handler=output_handler,
        process_start_handler=process_start_handler,
        authority=wiring.authority,
        notifier=wiring.notifier,
    )
    try:
        client.acquire()
    except BaseException:
        client.close()
        raise
    return client


def _open_helper_client_with_events(
    *events: bytes,
    wiring: NotifiedAuthority,
    later: tuple[bytes, ...] | None = None,
    process_start_handler: ProcessStartHandler | None = None,
) -> tuple[HelperConnection, HelperPeerProcess]:
    process = HelperPeerProcess(events, later=later)
    client = _open_helper_client(process, wiring, process_start_handler=process_start_handler)
    return client, process


# The open_client fixture: connect a helper client over a process double.
type OpenClient = Callable[[ControlProcess], HelperConnection]


@pytest.fixture
def open_client(
    notified_authority: NotifiedAuthority,
) -> Iterator[OpenClient]:
    opened: list[tuple[HelperConnection, ControlProcess]] = []

    def open_with(process: ControlProcess) -> HelperConnection:
        client = _open_helper_client(process, notified_authority)
        opened.append((client, process))
        return client

    yield open_with
    for client, process in opened:
        # Fault tests may leave the transport running; end its stream so the
        # acquisition reader stops before its pipes are released.
        process.end_events()
        assert client._join_reader(10)
        # Fault doubles may fail on close; teardown only releases descriptors.
        for release in (process.stdin.close, process.stdout.close, process.close_stderr):
            with suppress(Exception):
                release()


@pytest.fixture
def helper_client(
    open_client: OpenClient,
    notified_authority: NotifiedAuthority,
) -> tuple[HelperConnection, SessionAuthority]:
    return open_client(ControlProcess()), notified_authority.authority


def test_helper_client_sends_preference_and_reports_attempt_before_startup_error(
    notified_authority: NotifiedAuthority,
) -> None:
    observed: list[tuple[str, ...]] = []
    first = ("openocd", "-c", "bindto 127.64.0.1", "")
    client, helper_process = _open_helper_client_with_events(
        encode_frame("ATTEMPT", argv=list(first)),
        encode_frame("CHILD_OUTPUT", stream="stderr", payload="bind collision", line_end=True),
        terminal_message(
            Trigger.HELPER_FAILURE, code="FAILED", message="startup failed", child_acquired=True
        ),
        process_start_handler=observed.append,
        wiring=notified_authority,
    )
    try:
        with pytest.raises(SessionError):
            client.start_process(ProcessSpec(("openocd",)), (), preferred_address="127.64.0.1")
        request = decode_command(bytes(helper_process.written))
        assert request.preferred_address == "127.64.0.1"
        assert observed == [first]
    finally:
        client.close()


def test_explicit_session_values_are_not_recursively_expanded() -> None:
    process = ProcessSpec(
        ("openocd", "preview"),
        argv_templates=(
            (
                1,
                ArgumentTemplate(
                    (
                        "load_image ",
                        TclWord(
                            (
                                SessionValue.WORKSPACE,
                                "/",
                                SessionValue.ADDRESS,
                                "/{workspace}/{address}/firmware.bin",
                            )
                        ),
                        " 0x1000",
                    )
                ),
            ),
        ),
    )
    stream = io.BytesIO()
    write_start(stream, process, ())
    request = decode_command(stream.getvalue())
    argv = materialize_argv(
        request.argv,
        workspace=r'/runtime/[review] $value "quoted" \backslash {workspace} {address}/session',
        address="127.64.0.1",
        argv_templates=request.argv_templates,
    )
    assert argv[1] == (
        r'load_image "/runtime/\[review\] \$value \"quoted\" \\backslash '
        r'\{workspace\} \{address\}/session/127.64.0.1/'
        r'\{workspace\}/\{address\}/firmware.bin" 0x1000'
    )


@pytest.mark.parametrize("image_type", ("hex", "bin", "elf"))
@pytest.mark.parametrize(
    "workspace, mapped_root, expected_path_root",
    (
        (
            r'/runtime/[review] $value "quoted" \backslash {workspace} {address}/session',
            None,
            r'/runtime/\[review\] \$value \"quoted\" \\backslash '
            r'\{workspace\} \{address\}/session',
        ),
        ("/workspace", "/shared/{address}", r"/shared/\{address\}"),
        ("/workspace", "/shared/{workspace}", r"/shared/\{workspace\}"),
    ),
)
def test_flash_paths_are_quoted_after_session_allocation(
    notified_authority: NotifiedAuthority,
    tmp_path: Path,
    image_type: str,
    workspace: str,
    mapped_root: str | None,
    expected_path_root: str,
) -> None:
    image = tmp_path / f"firmware.{image_type}"
    if image_type == "elf":
        shutil.copyfile(sys.executable, image)
    else:
        image.write_bytes(b":00000001FF\n")
    config = tmp_path / "board.cfg"
    config.write_text("# fixture\n")
    config_remote = r'/configs/[review] $value "quoted" \backslash {workspace} {address}/board.cfg'
    executable = r'/tools/[review] $value "quoted" \backslash {workspace} {address}/openocd'
    user_tcl = 'puts "user [expr {1 + 2}] $value {workspace} {address}"'
    mappings: tuple[PathMapping, ...] = (PathMapping(config, PurePosixPath(config_remote)),)
    if mapped_root is not None:
        mappings += (PathMapping(tmp_path, PurePosixPath(mapped_root)),)
    plan = build_flash_plan(
        FlashInputs(
            executable=executable,
            image_type=image_type,
            file=str(image),
            elf_file=None,
            hex_file=None,
            bin_file=None,
            search_paths=(),
            config_files=(str(config),),
            pre_init=(user_tcl,),
            load_command="load_image",
            verify_command="verify_image",
            flash_address="0x1000",
            verify=True,
        ),
        PathPlanner(mappings),
    )
    process = HelperPeerProcess(
        (
            terminal_message(
                Trigger.HELPER_FAILURE, code="FAILED", message="controlled startup failure"
            ),
        ),
        initial=encode_frame(
            "SESSION_CREATED", helper="fake", session_id="session", remote_workspace=workspace
        ),
    )
    authority = notified_authority.authority
    client = _open_helper_client(process, notified_authority)
    try:
        assert client.start_process(plan.process, ()) is None
        assert client._join_reader()
        with pytest.raises(SessionError):
            authority.operation_status()
        request = decode_command(process.written.splitlines(keepends=True)[0])
        argv = materialize_argv(
            request.argv,
            workspace=workspace,
            address="127.64.0.1",
            literal_prefix=request.literal_prefix,
            argv_templates=request.argv_templates,
        )
    finally:
        client.close()

    if mapped_root is None:
        staged_images = [item for item in plan.staged_files if item.source == image]
        assert len(staged_images) == 1
        expected_image_path = f"{expected_path_root}/staged/{staged_images[0].destination}"
    else:
        expected_image_path = f"{expected_path_root}/firmware.{image_type}"

    assert argv[0] == executable
    assert argv[argv.index("-f") + 1] == config_remote
    assert "bindto 127.64.0.1" in argv
    if mapped_root is not None:
        checked_paths = tuple(
            materialize_path(check.path, workspace=workspace, address="127.64.0.1")
            for check in request.required_paths
        )
        assert f"{mapped_root}/firmware.{image_type}" in checked_paths
    assert user_tcl in argv
    commands = [
        argv[index + 1]
        for index, argument in enumerate(argv[:-1])
        if argument == "-c" and argv[index + 1].startswith(("load_image ", "verify_image "))
    ]
    suffix = " 0x1000" if image_type == "bin" else ""
    assert commands == [
        f'load_image "{expected_image_path}"{suffix}',
        f'verify_image "{expected_image_path}"{suffix}',
    ]


def test_open_rejects_initial_frame_without_lf(notified_authority: NotifiedAuthority) -> None:
    payload = CREATED.rstrip(b"\n")
    process = ControlProcess(payload)
    process.end_events()

    with pytest.raises(SessionError) as raised:
        _open_helper_client(process, notified_authority)
    assert isinstance(raised.value.__cause__, ValueError)


@pytest.mark.parametrize("policy", tuple(CompletionPolicy))
@pytest.mark.parametrize("contradiction", ("child", "workspace"))
def test_contradictory_terminal_is_reader_failure_and_never_published(
    notified_authority: NotifiedAuthority, policy: CompletionPolicy, contradiction: str
) -> None:
    terminal = decode_frame(
        terminal_message(Trigger.CONTROLLER_EOF, child_acquired=contradiction != "child")
    )
    if contradiction == "workspace":
        terminal["cleanup"]["workspace_disposal"] = "not_created"
    evidence = (
        encode_frame("READY", remote_address="127.64.0.1", child_pid=1)
        if policy == CompletionPolicy.LIVE_SERVER
        else encode_frame("CHILD_OUTPUT", stream="stdout", payload="started", line_end=True)
    )
    authority = notified_authority.authority
    client, process = _open_helper_client_with_events(
        encode_frame("ATTEMPT", argv=["child"]),
        evidence,
        later=(
            encode_frame(
                "SESSION_ENDED",
                **{k: v for k, v in terminal.items() if k not in {"version", "type"}},
            ),
        ),
        wiring=notified_authority,
    )
    try:
        client.start_process(ProcessSpec(("child",), completion_policy=policy), ())
        process.release_later()
        assert client._join_reader(5)
        with pytest.raises(SessionError) as reported:
            authority.operation_status()
        assert isinstance(reported.value.__cause__, ProtocolError)
        assert authority.snapshot().terminal is None
        client.close()
        assert authority.snapshot().failure is reported.value
        assert any(detail.code == "MISSING_TERMINAL" for detail in authority.snapshot().diagnostics)
        assert process.stdin.closed
        assert process.stdout.closed
    finally:
        client.close()


@pytest.mark.timeout(10)
def test_session_observations_project_exit_while_reader_remains_alive(
    monkeypatch: pytest.MonkeyPatch,
    open_client: OpenClient,
    notified_authority: NotifiedAuthority,
) -> None:
    authority = notified_authority.authority
    reader_blocked = threading.Event()
    release_reader = threading.Event()
    wait = connection_module.wait_for
    process = HelperPeerProcess(
        (
            encode_frame("ATTEMPT", argv=["child"]),
            terminal_message(Trigger.CHILD_EXIT, returncode=OPENOCD_FAILURE_RC),
        ),
        later=(),
    )

    def hold_after_terminal(
        *,
        readable: Iterable[Descriptor] = (),
        writable: Iterable[Descriptor] = (),
        sources: WaitSources = _NO_WAIT_SOURCES,
        deadline: float | None = None,
    ) -> WaitResult:
        # Block the actual production reader at its next OS wait, after its
        # real pipe/frame path has published terminal state to the authority.
        if readable == (process.stdout.fileno(),) and authority.snapshot().terminal:
            reader_blocked.set()
            assert release_reader.wait(5)
        return wait(readable=readable, writable=writable, sources=sources, deadline=deadline)

    monkeypatch.setattr(connection_module, "wait_for", hold_after_terminal)
    client = open_client(process)
    try:
        with pytest.raises(SessionError):
            client.start_process(ProcessSpec(("child",)), ())
        assert reader_blocked.wait(5)
        assert _child_returncode(authority) == OPENOCD_FAILURE_RC
        with pytest.raises(SessionError, match="exit status 7"):
            authority.operation_status()
    finally:
        release_reader.set()
        client.close()


def test_close_reaps_natural_exit_before_transport_cleanup(
    open_client: OpenClient, notified_authority: NotifiedAuthority
) -> None:
    authority = notified_authority.authority
    natural_exit_reaped = threading.Event()

    class Process(ControlProcess):
        @override
        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                # Forced termination must not stand in for reaping the natural exit.
                natural_exit_reaped.set()
            return super().wait(timeout)

    process = Process()
    helper_client = open_client(process)

    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))

    helper_client.close()
    error = authority.snapshot().failure

    assert error is None
    assert natural_exit_reaped.is_set()
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_large_start_is_rejected_before_helper_write(notified_authority: NotifiedAuthority) -> None:
    helper_client, process = _open_helper_client_with_events(
        terminal_message(Trigger.CONTROLLER_EOF, returncode=None),
        wiring=notified_authority,
    )
    # JSON escaping makes the encoded frame oversized even though its argv
    # contains fewer than 1 MiB of UTF-8 bytes.
    request = ProcessSpec(("child", "é" * 200_000))
    try:
        with pytest.raises(ProtocolError):
            helper_client.start_process(request, ())
    finally:
        helper_client.close()
    assert process.written == b""


def test_helper_shutdown_retains_established_failure(notified_authority: NotifiedAuthority) -> None:
    authority = notified_authority.authority
    helper_client, process = _open_helper_client_with_events(
        terminal_message(Trigger.HELPER_FAILURE, code="FAILED", message="startup failed"),
        wiring=notified_authority,
    )

    with pytest.raises(SessionError, match="startup failed"):
        helper_client.start_process(ProcessSpec(("child",)), ())

    with pytest.raises(SessionError) as reported:
        authority.operation_status()
    helper_client.close()
    error = authority.snapshot().failure

    assert error is reported.value
    commands = [
        decode_frame(bytes(line))["type"] for line in process.written.splitlines(keepends=True)
    ]
    assert commands == ["START"]


@pytest.mark.timeout(10)
def test_close_releases_live_lease_before_input_eof(notified_authority: NotifiedAuthority) -> None:
    helper_client, process = _open_helper_client_with_events(
        encode_frame("ATTEMPT", argv=["child"]),
        encode_frame("READY", remote_address="127.64.0.1", child_pid=1),
        later=(),
        wiring=notified_authority,
    )
    helper_client.start_process(ProcessSpec(("child",)), ())
    helper_client.close()
    commands = [
        decode_frame(bytes(line))["type"] for line in process.written.splitlines(keepends=True)
    ]
    assert commands == ["START", "RELEASE"]


class _InputEndingHelper(ControlProcess):
    """A helper whose input may end before its terminal is delivered at exit."""

    def __init__(self, terminal: bytes) -> None:
        super().__init__()
        read_fd, write_fd = os.pipe()
        self.stdin = os.fdopen(write_fd, "wb")
        self.controller_input = os.fdopen(read_fd, "rb")
        self._terminal = terminal

    def deliver_terminal(self) -> None:
        if self._terminal:
            self.emit(self._terminal)
            self._terminal = b""

    @override
    def wait(self, timeout: float | None = None) -> int:
        self.deliver_terminal()
        return super().wait(timeout)


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    ("ending", "returncode"),
    (
        ("terminal-before-close", 0),
        ("input-ended-before-terminal", 0),
        ("input-ended-before-terminal", OPENOCD_FAILURE_RC),
    ),
)
def test_unneeded_or_undeliverable_release_keeps_one_shot_terminal_result(
    notified_authority: NotifiedAuthority, ending: str, returncode: int
) -> None:
    authority = notified_authority.authority
    process = _InputEndingHelper(
        encode_frame("ATTEMPT", argv=["child"])
        + terminal_message(Trigger.CHILD_EXIT, returncode=returncode)
    )
    client = _open_helper_client(process, notified_authority)
    try:
        client.start_process(
            ProcessSpec(("child",), completion_policy=CompletionPolicy.PROCESS_EXIT), ()
        )
        start = process.controller_input.readline()
        if ending == "terminal-before-close":
            process.deliver_terminal()
            while authority.snapshot().terminal is None:
                wait_for(sources=WaitSources(notifiers=(notified_authority.notifier,)))
        else:
            # The helper exited after natural completion; RELEASE meets EPIPE.
            process.controller_input.close()
        client.close()
    finally:
        process.end_events()
        assert client._join_reader(10)
    snapshot = authority.snapshot()
    assert decode_frame(start)["type"] == "START"
    if ending == "terminal-before-close":
        # An ended session needs no release.
        assert process.controller_input.read() == b""
    assert {detail.code for detail in snapshot.diagnostics} <= {"TERMINAL_FAILURE"}
    if returncode == 0:
        assert authority.operation_status() == 0
    else:
        with pytest.raises(SessionError, match=f"exit status {returncode}"):
            authority.operation_status()


@pytest.mark.timeout(10)
def test_controller_eof_terminal_prevents_dependent_entry(
    notified_authority: NotifiedAuthority,
) -> None:
    authority = notified_authority.authority
    helper_client, process = _open_helper_client_with_events(
        encode_frame("ATTEMPT", argv=["child"]),
        encode_frame("READY", remote_address="127.64.0.1", child_pid=1),
        later=(terminal_message(Trigger.CONTROLLER_EOF, returncode=None, child_acquired=True),),
        wiring=notified_authority,
    )
    assert helper_client.start_process(ProcessSpec(("child",)), ()) == "127.64.0.1"
    process.release_later()
    assert helper_client._join_reader(5)

    try:
        with pytest.raises(SessionError) as raised:
            authority.admit_work((), ())

        assert raised.value.__cause__ is None
    finally:
        helper_client.close()


@pytest.mark.timeout(10)
def test_helper_shutdown_retains_observed_background_failure(
    notified_authority: NotifiedAuthority,
) -> None:
    authority = notified_authority.authority
    helper_client, process = _open_helper_client_with_events(
        encode_frame("ATTEMPT", argv=["child"]),
        encode_frame("READY", remote_address="127.64.0.1", child_pid=1),
        later=(
            terminal_message(
                Trigger.HELPER_FAILURE,
                code="FAILED",
                message="background failed",
                child_acquired=True,
            ),
        ),
        wiring=notified_authority,
    )
    helper_client.start_process(ProcessSpec(("child",)), ())
    process.release_later()
    assert helper_client._join_reader(5)

    for _attempt in range(2):
        with pytest.raises(SessionError) as reported:
            authority.operation_status()

    helper_client.close()
    assert authority.snapshot().failure is reported.value
    commands = [
        decode_frame(bytes(line))["type"] for line in process.written.splitlines(keepends=True)
    ]
    assert commands == ["START"]


@pytest.mark.parametrize("cleanup_fails", (False, True), ids=("clean", "cleanup-failure"))
@pytest.mark.timeout(10)
def test_close_records_background_and_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch, notified_authority: NotifiedAuthority, cleanup_fails: bool
) -> None:
    authority = notified_authority.authority
    helper_client, process = _open_helper_client_with_events(
        encode_frame("ATTEMPT", argv=["child"]),
        encode_frame("READY", remote_address="127.64.0.1", child_pid=1),
        later=(
            terminal_message(
                Trigger.HELPER_FAILURE,
                code="FAILED",
                message="background failed",
                child_acquired=True,
            ),
        ),
        wiring=notified_authority,
    )
    helper_client.start_process(ProcessSpec(("child",)), ())
    process.release_later()
    assert helper_client._join_reader(5)

    cleanup_error = RuntimeError("forced cleanup failed")

    def fail_stop(_process: ProcessControl, *, close_streams: bool = True) -> None:
        del close_streams
        raise cleanup_error

    if cleanup_fails:
        monkeypatch.setattr(connection_module, "stop_process", fail_stop)

    helper_client.close()
    error = authority.snapshot().failure

    assert isinstance(error, SessionError)
    assert "background failed" in str(error)
    assert error.__cause__ is None
    if cleanup_fails:
        assert any(str(cleanup_error) in note for note in error.__notes__)
    commands = [
        decode_frame(bytes(line))["type"] for line in process.written.splitlines(keepends=True)
    ]
    assert commands == ["START"]


def test_reader_failure_takes_precedence_over_known_openocd_result(
    helper_client: tuple[HelperConnection, SessionAuthority],
) -> None:
    _client, authority = helper_client
    reader_error = RuntimeError("protocol failed")
    authority.record_reader_failure(reader_error)
    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=OPENOCD_FAILURE_RC))

    assert _child_returncode(authority) == OPENOCD_FAILURE_RC
    with pytest.raises(SessionError) as raised:
        authority.operation_status()

    assert raised.value.__cause__ is reader_error
    assert any("remote OpenOCD failed" in note for note in raised.value.__notes__)


@pytest.mark.parametrize(
    ("policy", "returncode", "succeeds"),
    (
        (CompletionPolicy.PROCESS_EXIT, 0, False),
        (CompletionPolicy.PROCESS_EXIT, None, False),
        (CompletionPolicy.LIVE_SERVER, 0, True),
    ),
)
def test_one_shot_result_requires_genuine_child_completion(
    notified_authority: NotifiedAuthority,
    policy: CompletionPolicy,
    returncode: int | None,
    succeeds: bool,
) -> None:
    authority = notified_authority.authority
    events: tuple[bytes, ...] = (encode_frame("ATTEMPT", argv=["child"]),)
    if policy == CompletionPolicy.LIVE_SERVER:
        events += (encode_frame("READY", remote_address="127.64.0.1", child_pid=1),)
    terminal = terminal_message(
        Trigger.CONTROLLER_EOF,
        returncode=returncode,
        termination_requested=True,
        child_acquired=True,
    )
    client, process = _open_helper_client_with_events(
        *events, wiring=notified_authority, later=(terminal,)
    )
    try:
        client.start_process(ProcessSpec(("child",), completion_policy=policy), ())
        # Orderly local closure disposes of the child; only live-server work
        # may treat that requested termination as a normal end.
        authority.cancel()
        process.release_later()
        assert client._join_reader(5)
        if succeeds:
            assert authority.operation_status() == returncode
        else:
            with pytest.raises(SessionError):
                authority.operation_status()
    finally:
        client.close()


@pytest.mark.parametrize("terminal_failure", ("startup", "child"))
@pytest.mark.parametrize("boundary", ("status", "close"))
@pytest.mark.parametrize("trailing", ("malformed", "illegal_frame"))
def test_terminal_failure_stays_primary_after_trailing_protocol_failure(
    notified_authority: NotifiedAuthority, terminal_failure: str, boundary: str, trailing: str
) -> None:
    terminal = (
        terminal_message(Trigger.STARTUP_FAILURE, code="STARTUP_FAILURE", message="startup failed")
        if terminal_failure == "startup"
        else terminal_message(Trigger.CHILD_EXIT, returncode=OPENOCD_FAILURE_RC)
    )
    corruption = (
        b"not-json\n" if trailing == "malformed" else encode_frame("ATTEMPT", argv=["unadmitted"])
    )
    authority = notified_authority.authority
    client, _process = _open_helper_client_with_events(
        encode_frame("ATTEMPT", argv=["child"]), terminal, corruption, wiring=notified_authority
    )
    try:
        client.start_process(
            ProcessSpec(("child",), completion_policy=CompletionPolicy.PROCESS_EXIT), ()
        )
        assert client._join_reader(5)  # Both facts are recorded before the first check.
        error: BaseException | None
        if boundary == "status":
            with pytest.raises(SessionError) as raised:
                authority.operation_status()
            error = raised.value
            client.close()
            assert authority.snapshot().failure is error
        else:
            client.close()
            error = authority.snapshot().failure
        assert isinstance(error, SessionError)
        assert (
            "startup failed" if terminal_failure == "startup" else "remote OpenOCD failed"
        ) in str(error)
        assert error.__cause__ is None
        assert any("helper event stream failed" in note for note in error.__notes__)
        terminal_snapshot = authority.snapshot().terminal
        assert terminal_snapshot is not None
        child_result = terminal_snapshot.outcome.child_result
        assert (None if child_result is None else child_result.returncode) == (
            None if terminal_failure == "startup" else OPENOCD_FAILURE_RC
        )
    finally:
        client.close()


@pytest.mark.parametrize("terminal_failure", ("startup", "child"))
@pytest.mark.parametrize("shutdown_failure", ("stdin", "wait"))
def test_close_preserves_terminal_failure_before_shutdown(
    open_client: OpenClient,
    notified_authority: NotifiedAuthority,
    terminal_failure: str,
    shutdown_failure: str,
) -> None:
    authority = notified_authority.authority
    shutdown_error = OSError(f"helper {shutdown_failure} failed")
    shutdown_error.add_note("shutdown failure detail")
    terminal = (
        terminal_snapshot(Trigger.STARTUP_FAILURE, code="STARTUP_FAILURE", message="startup failed")
        if terminal_failure == "startup"
        else terminal_snapshot(Trigger.CHILD_EXIT, returncode=OPENOCD_FAILURE_RC)
    )

    class FailingInput(io.BytesIO):
        def close(self) -> None:
            was_closed = self.closed
            super().close()
            if shutdown_failure == "stdin" and not was_closed:
                raise shutdown_error

    class Process(ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdin = FailingInput()

        @override
        def wait(self, timeout: float | None = None) -> int:
            if shutdown_failure == "wait" and timeout == connection_module.HELPER_STOP_TIMEOUT:
                raise shutdown_error
            return super().wait(timeout)

    process = Process()
    helper_client = open_client(process)
    authority.record_terminal(terminal)

    helper_client.close()
    error = authority.snapshot().failure

    assert isinstance(error, SessionError)
    assert ("startup failed" if terminal_failure == "startup" else "remote OpenOCD failed") in str(
        error
    )
    assert any(str(shutdown_error) in note for note in error.__notes__)
    assert any("shutdown failure detail" in note for note in error.__notes__)
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_close_keeps_shutdown_failure_primary_when_forced_cleanup_also_fails(
    open_client: OpenClient, notified_authority: NotifiedAuthority
) -> None:
    authority = notified_authority.authority
    graceful_stop_error = RuntimeError("graceful stop failed")
    forced_stop_error = RuntimeError("forced stop failed")

    class FailingStdin(io.BytesIO):
        def close(self) -> None:
            super().close()
            raise graceful_stop_error

    class Process(ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdin = FailingStdin()

        @override
        def wait(self, timeout: float | None = None) -> int:
            if timeout == connection_module.HELPER_STOP_TIMEOUT:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return super().wait(timeout)

        @override
        def terminate(self) -> None:
            raise forced_stop_error

    helper_client = open_client(Process())

    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))
    helper_client.close()
    error = authority.snapshot().failure

    assert error is graceful_stop_error
    assert any("15.0" in note for note in graceful_stop_error.__notes__)
    assert any(str(forced_stop_error) in note for note in graceful_stop_error.__notes__)


def test_close_cleans_up_helper_when_initial_status_observation_fails(
    open_client: OpenClient, notified_authority: NotifiedAuthority
) -> None:
    authority = notified_authority.authority
    observation_error = RuntimeError("helper status failed")

    class Process(ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.initial_status_failure_pending = True

        @override
        def poll(self) -> int | None:
            if self.initial_status_failure_pending:
                self.initial_status_failure_pending = False
                raise observation_error
            return self.returncode

    process = Process()
    helper_client = open_client(process)

    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))
    helper_client.close()
    error = authority.snapshot().failure

    assert error is observation_error
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_close_cleans_up_helper_when_reader_join_fails(
    open_client: OpenClient, notified_authority: NotifiedAuthority
) -> None:
    authority = notified_authority.authority
    join_error = RuntimeError("helper reader join failed")

    class Reader(threading.Thread):
        def __init__(self, reader: threading.Thread) -> None:
            super().__init__()
            self.reader = reader
            self.join_failure_pending = True

        @override
        def join(self, timeout: float | None = None) -> None:
            if self.join_failure_pending:
                self.join_failure_pending = False
                raise join_error
            self.reader.join(timeout)

        @override
        def is_alive(self) -> bool:
            return self.join_failure_pending or self.reader.is_alive()

    process = ControlProcess()
    helper_client = open_client(process)
    assert helper_client._reader_thread is not None
    helper_client._reader_thread = Reader(helper_client._reader_thread)
    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))

    helper_client.close()
    error = authority.snapshot().failure

    assert error is join_error
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_close_preserves_cleanup_error_when_final_status_observation_fails(
    open_client: OpenClient, notified_authority: NotifiedAuthority
) -> None:
    authority = notified_authority.authority
    status_error = RuntimeError("helper final status failed")
    cleanup_error = RuntimeError("helper stderr cleanup failed")

    class Process(ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = 0
            self.cleanup_attempted = False

        @override
        def poll(self) -> int | None:
            if self.cleanup_attempted:
                raise status_error
            return self.returncode

        @override
        def close_stderr(self) -> None:
            self.cleanup_attempted = True
            raise cleanup_error

    process = Process()
    helper_client = open_client(process)
    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))

    helper_client.close()
    error = authority.snapshot().failure

    assert error is cleanup_error
    assert process.stdin.closed
    assert process.stdout.closed
    assert any(str(status_error) in note for note in cleanup_error.__notes__)


@pytest.mark.parametrize("control_status", (0, 23))
def test_failed_reader_start_rolls_back_control_acquisition(
    monkeypatch: pytest.MonkeyPatch, notified_authority: NotifiedAuthority, control_status: int
) -> None:
    authority = notified_authority.authority
    reader_start_error = RuntimeError("helper reader did not start")

    def fail_start(_thread: threading.Thread) -> None:
        raise reader_start_error

    process = ControlProcess()
    process.returncode = control_status
    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError) as raised:
        _open_helper_client(process, notified_authority)

    assert raised.value is reader_start_error
    assert authority.snapshot().failure is reader_start_error
    assert process.returncode == control_status
    assert any(
        diagnostic.code == "CONTROL_PROCESS_FAILURE"
        for diagnostic in authority.snapshot().diagnostics
    ) == bool(control_status)
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize(
    ("wait_failure_type", "stdin_close_fails"),
    (
        pytest.param(subprocess.TimeoutExpired, False, id="timeout"),
        pytest.param(asyncio.CancelledError, False, id="cancelled"),
        pytest.param(KeyboardInterrupt, False, id="interrupted"),
        pytest.param(subprocess.TimeoutExpired, True, id="timeout-with-stdin-close-error"),
    ),
)
@pytest.mark.parametrize("terminal_fails", (False, True))
def test_close_forces_cleanup_after_helper_wait_failure(
    open_client: OpenClient,
    notified_authority: NotifiedAuthority,
    wait_failure_type: type[BaseException],
    stdin_close_fails: bool,
    terminal_fails: bool,
) -> None:
    authority = notified_authority.authority
    close_event = (
        terminal_message(Trigger.STARTUP_FAILURE, code="STARTUP_FAILURE", message="startup failed")
        if terminal_fails
        else terminal_message(Trigger.CONTROLLER_EOF, returncode=None)
    )
    stdin_close_error = OSError("helper stdin close failed")
    wait_error = (
        subprocess.TimeoutExpired(("fake-helper",), connection_module.HELPER_STOP_TIMEOUT)
        if wait_failure_type is subprocess.TimeoutExpired
        else wait_failure_type()
    )

    class StopInput(io.BytesIO):
        def __init__(self, helper: ControlProcess) -> None:
            super().__init__()
            self.helper = helper

        def close(self) -> None:
            if not self.closed and not terminal_fails:
                # Controller EOF makes the helper report and end its stream.
                self.helper.emit(close_event)
                self.helper.end_events()
            super().close()
            if stdin_close_fails:
                raise stdin_close_error

    class Process(ControlProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdin = StopInput(self)

        @override
        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None and self.returncode is None:
                raise wait_error
            return super().wait(timeout)

        @override
        def terminate(self) -> None:
            # A failed helper reports only after forced cleanup, so the shutdown
            # failures above are deterministically recognized first.
            if terminal_fails:
                self.emit(close_event)
            super().terminate()

    process = Process()
    helper_client = open_client(process)

    helper_client.close()
    error = authority.snapshot().failure

    assert error is (stdin_close_error if stdin_close_fails else wait_error)
    if stdin_close_fails:
        assert any(str(wait_error) in note for note in stdin_close_error.__notes__)
    if terminal_fails:
        assert error is not None
        assert any("startup failed" in note for note in error.__notes__)
    assert process.returncode == 0
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_helper_close_keeps_reader_owned_stdout_open_until_reader_stops(
    open_client: OpenClient, notified_authority: NotifiedAuthority
) -> None:
    authority = notified_authority.authority
    readers: list[threading.Thread] = []
    closed_after_reader: list[bool] = []

    class ReaderOwnedStream(io.BufferedReader):
        def close(self) -> None:
            if not self.closed:
                closed_after_reader.append(not readers[0].is_alive())
            super().close()

    class Process(ControlProcess):
        @staticmethod
        @override
        def _open_events(read_fd: int) -> BinaryIO:
            return ReaderOwnedStream(io.FileIO(read_fd, "rb"))

    process = Process()
    helper = open_client(process)
    assert helper._reader_thread is not None
    readers.append(helper._reader_thread)
    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))

    helper.close()
    error = authority.snapshot().failure

    assert error is None
    assert closed_after_reader == [True]
    assert process.stdin.closed
    assert process.stdout.closed
    assert process.stderr.closed


def test_helper_close_retains_nested_process_cleanup_diagnostics(
    open_client: OpenClient, notified_authority: NotifiedAuthority
) -> None:
    authority = notified_authority.authority
    terminate_error = RuntimeError("helper terminate failed")
    stderr_error = RuntimeError("helper stderr close failed")

    class Process(ControlProcess):
        @override
        def wait(self, timeout: float | None = None) -> int:
            if timeout == connection_module.HELPER_STOP_TIMEOUT:
                raise SessionError("shutdown wait failed")
            return super().wait(timeout)

        @override
        def terminate(self) -> None:
            super().terminate()
            raise terminate_error

        @override
        def close_stderr(self) -> None:
            raise stderr_error

    helper = open_client(Process())
    authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=0))

    helper.close()
    error = authority.snapshot().failure

    assert isinstance(error, SessionError)
    notes = error.__notes__
    assert any("helper terminate failed" in note for note in notes)
    assert any("helper stderr close failed" in note for note in notes)


@pytest.mark.timeout(60)
def test_helper_startup_timeout_does_not_block_on_partial_output(
    monkeypatch: pytest.MonkeyPatch, notified_authority: NotifiedAuthority
) -> None:
    class ElapsedStartupClock:
        # Startup began one full timeout ago, so the production deadline has
        # already passed when the coordinator waits.
        @staticmethod
        def monotonic() -> float:
            return time.monotonic() - connection_module.HELPER_START_TIMEOUT

    process = ControlProcess(CREATED.rstrip(b"\n"))
    real_wait = connection_module.wait_for
    reader_waits = 0
    partial_held = threading.Event()

    def wait_with_partial_frame_held(**kwargs: Any) -> Any:
        nonlocal reader_waits
        if kwargs.get("readable") == (process.stdout.fileno(),):
            # The reader's second wait follows its read of the partial frame.
            reader_waits += 1
            if reader_waits == 2:
                partial_held.set()
        elif kwargs.get("deadline") is not None:
            assert partial_held.wait(10), "reader did not consume the partial frame"
        return real_wait(**kwargs)

    monkeypatch.setattr(connection_module, "time", ElapsedStartupClock)
    monkeypatch.setattr(connection_module, "wait_for", wait_with_partial_frame_held)
    try:
        with pytest.raises(SessionError) as raised:
            _open_helper_client(process, notified_authority)

        assert isinstance(raised.value.__cause__, TimeoutError)
        assert process.returncode is not None
        assert process.stdout.closed
    finally:
        process.end_events()


@pytest.mark.timeout(10)
def test_helper_client_output_delivery_does_not_retain_event_history(
    notified_authority: NotifiedAuthority,
) -> None:
    authority = notified_authority.authority
    payloads = [f"payload-{index}" for index in range(1024)]
    process = HelperPeerProcess(
        (
            encode_frame("ATTEMPT", argv=["child"]),
            encode_frame("READY", remote_address="127.64.0.1", child_pid=1),
            *(
                encode_frame(
                    "CHILD_OUTPUT",
                    stream="stdout" if index % 2 == 0 else "stderr",
                    payload=payload,
                    line_end=False,
                )
                for index, payload in enumerate(payloads)
            ),
        ),
        later=(terminal_message(Trigger.CHILD_EXIT, returncode=0),),
    )
    handled: list[tuple[str, str, bool]] = []
    helper_client = _open_helper_client(
        process,
        notified_authority,
        output_handler=lambda stream, payload, line_end: handled.append(
            (stream, payload, line_end)
        ),
    )
    try:
        helper_client.start_process(ProcessSpec(("child",)), ())
        # Keep the live peer open until READY is admitted; output volume must
        # not make success depend on the coordinator outrunning transport EOF.
        process.release_later()
        assert helper_client._join_reader(10)
        assert handled == [
            ("stdout" if index % 2 == 0 else "stderr", payload, False)
            for index, payload in enumerate(payloads)
        ]
        assert authority.operation_status() == 0
    finally:
        helper_client.close()
        assert authority.snapshot().failure is None


@pytest.mark.parametrize("child_first", (True, False), ids=("child-first", "transport-first"))
def test_child_and_control_status_use_one_persistent_arbitration_path(
    open_client: OpenClient,
    notified_authority: NotifiedAuthority,
    child_first: bool,
) -> None:
    authority = notified_authority.authority
    process = ControlProcess()
    helper_client = open_client(process)
    authority.record_ready()
    if child_first:
        authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=7))
    process.returncode = 1
    # Exercise failure composition independently of physical reader settlement;
    # real transport observation now accounts final protocol bytes first.
    authority.record_control_status(process.returncode)
    with pytest.raises(SessionError) as initial:
        authority.operation_status()
    assert ("exit status 7" if child_first else "status 1") in str(initial.value)
    with pytest.raises(SessionError) as first_gate:
        authority.admit_work((), ())
    assert first_gate.value is initial.value
    if not child_first:
        authority.record_terminal(terminal_snapshot(Trigger.CHILD_EXIT, returncode=7))
    authority.record_control_status(process.returncode)
    with pytest.raises(SessionError) as later:
        authority.operation_status()
    assert later.value is initial.value
    assert ("exit status 7" if child_first else "status 1") in str(initial.value)
    assert any(
        ("status 1" if child_first else "exit status 7") in note for note in initial.value.__notes__
    )
    assert _child_returncode(authority) == 7
    with pytest.raises(SessionError) as gate:
        authority.admit_work((), ())
    assert gate.value is initial.value
    helper_client.close()
    assert authority.snapshot().failure is initial.value
    assert _child_returncode(authority) == 7
