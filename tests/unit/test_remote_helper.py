# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import errno
import fcntl
import importlib.util
import io
import json
import math
import os
import select
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Buffer, Callable
from contextlib import suppress
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import IO
from unittest.mock import create_autospec

import pytest
from zephyr_remote_openocd.remote.lifecycle import Closed
from zephyr_remote_openocd.remote.model import RemotePathCheck, RemoteProcess
from zephyr_remote_openocd.remote.outcome import CompletionPolicy, Trigger
from zephyr_remote_openocd.remote.protocol import write_start
from zephyr_remote_openocd.remote.wire import decode_terminal

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "zro_remote_helper", ROOT / "python/zephyr_remote_openocd/remote_helper.py"
)
assert SPEC is not None and SPEC.loader is not None
remote_helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remote_helper)

SAMPLE_CHILD_EXIT_CODE = 7
SignalHandler = Callable[[int, FrameType | None], object] | int | None


@pytest.fixture
def control_pipe(monkeypatch):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb", buffering=0) as writer:
        monkeypatch.setattr(remote_helper.sys, "stdin", SimpleNamespace(buffer=reader))
        yield reader, writer


def test_control_frames_retain_partial_bytes_without_waiting_for_lf():
    frames = remote_helper._ControlFrames()
    frames.feed(b'{"value":"\xc3')
    assert frames.pop_frame() is None
    frames.feed(b'\xa9"}')
    assert frames.pop_frame() is None
    frames.feed(b"\n")
    assert json.loads(frames.pop_frame()) == {"value": "é"}
    frames.finish()


def test_control_frames_keep_batched_frames_and_incomplete_tail():
    frames = remote_helper._ControlFrames()
    frames.feed(b'{"value":1}\n{"value":2}\n{"value":')
    assert json.loads(frames.pop_frame()) == {"value": 1}
    assert json.loads(frames.pop_frame()) == {"value": 2}
    assert frames.pop_frame() is None
    with pytest.raises(ValueError):
        frames.finish()


@pytest.mark.parametrize("payload", (b"x" * 32, b"x" * 32 + b"\n"))
def test_control_frames_reject_oversized_input(monkeypatch, payload):
    monkeypatch.setattr(remote_helper, "MAX_CONTROL_FRAME_SIZE", 32)
    frames = remote_helper._ControlFrames()
    frames.feed(payload)
    with pytest.raises(ValueError):
        frames.pop_frame()


def test_control_frame_limit_applies_to_individual_frames(monkeypatch):
    monkeypatch.setattr(remote_helper, "MAX_CONTROL_FRAME_SIZE", 32)
    frames = remote_helper._ControlFrames()
    frames.feed(b"x" * 31 + b"\n" + b"y" * 31 + b"\n" + b"z" * 31)
    assert frames.pop_frame() == b"x" * 31 + b"\n"
    assert frames.pop_frame() == b"y" * 31 + b"\n"
    assert frames.pop_frame() is None
    frames.feed(b"\n")
    assert frames.pop_frame() == b"z" * 31 + b"\n"
    frames.finish()


def _wait_for_descendant(path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            value = path.read_text(encoding="ascii")
            if value:
                return int(value)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.01)
    raise AssertionError("descendant PID was not published")


def _assert_pidfd_exited(pidfd, timeout=5):
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    if not poller.poll(timeout * 1000):
        raise AssertionError("process survived cleanup")


def _cleanup_test_child(child, descendant_pidfd):
    try:
        if child.poll() is None:
            asyncio.run(child.terminate())
        child.close_streams()
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        with suppress(BaseException):
            child.process.wait(timeout=5)
        with suppress(BaseException):
            child.close_streams()
    if descendant_pidfd is not None:
        with suppress(ProcessLookupError):
            signal.pidfd_send_signal(descendant_pidfd, signal.SIGKILL)


def _forking_child_code(exit_on_term):
    leader_signal = "SIGTERM" if exit_on_term else "SIGUSR1"
    leader_exit = f"""
def stop(_signal, _frame):
    raise SystemExit(0)

signal.signal(signal.{leader_signal}, stop)
"""
    parent_wait = "time.sleep(30)" if exit_on_term else "signal.pause()"
    return f"""
import os
import signal
import sys
import time

{leader_exit}
descendant = os.fork()
if descendant == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    descriptor = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(descriptor, str(os.getpid()).encode("ascii"))
    os.close(descriptor)
    os.close(1)
    os.close(2)
    time.sleep(30)
{parent_wait}
"""


@pytest.fixture
def start_command():
    return {
        "version": 2,
        "type": "START",
        "completion_policy": "live_server",
        "argv": ["openocd", "{address}"],
        "environment": {"ZRO_TEST": "value"},
        "required_paths": [{"kind": "file", "path": "{workspace}/image"}],
        "services": [
            {"name": "gdb", "remote_port": 3333},
            {"name": "tcl", "remote_port": 6333},
        ],
        "required_output_sentinels": ["READY"],
        "readiness_timeout": 30.0,
        "literal_prefix": 1,
        "argv_templates": [],
        "preferred_address": None,
    }


def _decode_chunks(chunks: tuple[bytes, ...], name: str, sentinels=None) -> None:
    if sentinels is None:
        sentinels = remote_helper._RequiredOutputSentinels(())
    decoder = remote_helper._OutputDecoder(name, sentinels)
    for chunk in (*chunks, b""):
        for fragment in decoder.feed(chunk):
            remote_helper.emit(
                "CHILD_OUTPUT",
                stream=fragment.stream,
                payload=fragment.payload,
                line_end=fragment.line_end,
            )


def test_relay_emits_short_fragment_before_complete_sentinel():
    sentinels = remote_helper._RequiredOutputSentinels(("READY",))
    decoder = remote_helper._OutputDecoder("stdout", sentinels)
    fragments = decoder.feed(b"READY")
    assert [(item.payload, item.line_end) for item in fragments] == [("READY", False)]
    assert not sentinels.ready
    fragments = decoder.feed(b"\n")
    assert [(item.payload, item.line_end) for item in fragments] == [("", True)]
    assert sentinels.ready


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("OPENOCD INIT", "STARTUP COMPLETE"),
        ("STARTUP COMPLETE", "OPENOCD INIT"),
    ),
    ids=("init-first", "startup-first"),
)
def test_relay_waits_for_each_complete_sentinel_across_streams(monkeypatch, first, second):
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 8)
    monkeypatch.setattr(remote_helper, "emit", lambda *_args, **_kwargs: None)
    required_output_sentinels = remote_helper._RequiredOutputSentinels(
        ("OPENOCD INIT", "STARTUP COMPLETE")
    )

    _decode_chunks(
        (b"diagnostic\n", first[:8].encode(), (first[8:] + "\n").encode()),
        "stdout",
        required_output_sentinels,
    )

    assert not required_output_sentinels.ready

    _decode_chunks(
        (("  " + second + "  \n").encode(),),
        "stderr",
        required_output_sentinels,
    )

    assert required_output_sentinels.ready


def test_decode_start_accepts_full_output_lines_with_internal_spaces(start_command):
    start_command["required_output_sentinels"] = ["READY FOR START"]

    request = remote_helper.decode_command(start_command)

    assert request.required_output_sentinels == ("READY FOR START",)


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (b"abcdefghij\n", "abcdefghij\n"),
        (b"tail", "tail"),
        (b"\n\nx\n", "\n\nx\n"),
        (b"", ""),
    ),
)
def test_relay_metadata_reconstructs_logical_output(monkeypatch, payload, expected):
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 4)
    events = []
    monkeypatch.setattr(
        remote_helper,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )

    _decode_chunks((payload,), "stdout")

    output_events = [values for _kind, values in events]
    reconstructed = "".join(
        event["payload"] + ("\n" if event["line_end"] else "") for event in output_events
    )
    assert reconstructed == expected
    assert all(event["stream"] == "stdout" for event in output_events)
    assert all(len(event["payload"]) <= remote_helper.RELAY_CHUNK_SIZE for event in output_events)
    assert all(event["payload"] or event["line_end"] for event in output_events)
    assert sum(event["line_end"] for event in output_events) == payload.count(b"\n")


def test_relay_preserves_split_utf8_and_invalid_bytes(monkeypatch):
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 3)
    events = []
    monkeypatch.setattr(
        remote_helper,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )

    _decode_chunks((b"utf \xe2", b"\x82", b"\xac\ninvalid \xff"), "stderr")

    output_events = [values for _kind, values in events]
    assert (
        "".join(event["payload"] + ("\n" if event["line_end"] else "") for event in output_events)
        == "utf €\ninvalid �"
    )
    assert all(len(event["payload"]) <= 3 for event in output_events)


@pytest.mark.parametrize("cleanup_fails", (False, True))
def test_spawn_child_rolls_back_process_when_ownership_wrapper_fails(monkeypatch, cleanup_fails):
    original_popen = remote_helper.subprocess.Popen
    processes = []
    process_pidfds = []
    failure = RuntimeError("injected child ownership failure")
    cleanup_errors = [OSError(f"child {stream} closure failed") for stream in ("stdout", "stderr")]

    def fail_close(stream, cleanup_error):
        close = stream.close

        def close_then_fail():
            close()
            raise cleanup_error

        monkeypatch.setattr(stream, "close", close_then_fail)

    def capture_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        process_pidfds.append(os.pidfd_open(process.pid))
        if cleanup_fails:
            for stream, cleanup_error in zip(
                (process.stdout, process.stderr), cleanup_errors, strict=True
            ):
                fail_close(stream, cleanup_error)
        return process

    def fail_child_ownership(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(remote_helper.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(remote_helper, "SupervisedChild", fail_child_ownership)
    try:
        with pytest.raises(RuntimeError) as raised:
            remote_helper._spawn_child((sys.executable, "-c", "import signal; signal.pause()"))

        assert raised.value is failure
        assert len(processes) == 1
        _assert_pidfd_exited(process_pidfds[0])
        assert processes[0].returncode is not None
        assert processes[0].stdout is not None and processes[0].stdout.closed
        assert processes[0].stderr is not None and processes[0].stderr.closed
        if cleanup_fails:
            for cleanup_error in cleanup_errors:
                assert any(str(cleanup_error) in note for note in failure.__notes__)
    finally:
        for process in processes:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with suppress(BaseException):
                    process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        for process_pidfd in process_pidfds:
            os.close(process_pidfd)


@pytest.mark.timeout(10)
def test_spawn_rollback_preserves_exited_leader_identity_until_group_signal(monkeypatch):
    popen = remote_helper.subprocess.Popen
    killpg = remote_helper.os.killpg
    failure = RuntimeError("supervisor construction failed after child exit")
    owner = remote_helper._ChildAcquisition(1)
    signalled = []

    def exited_process(*args, **kwargs):
        process = popen(*args, **kwargs)
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        return process

    def fail_construction(*_args, **_kwargs):
        raise failure

    def signal_owned_group(pid, signum):
        # Reaping before this boundary would release the identity and make this
        # observation fail, even though the acquisition ticket still owns it.
        if signum != 0:
            status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            assert status is not None and status.si_status == SAMPLE_CHILD_EXIT_CODE
            signalled.append(pid)
        killpg(pid, signum)

    monkeypatch.setattr(remote_helper.subprocess, "Popen", exited_process)
    monkeypatch.setattr(remote_helper, "SupervisedChild", fail_construction)
    monkeypatch.setattr(remote_helper.os, "killpg", signal_owned_group)
    with pytest.raises(RuntimeError) as raised:
        remote_helper._spawn_child(
            (sys.executable, "-c", f"import sys;sys.exit({SAMPLE_CHILD_EXIT_CODE})"),
            ownership=owner,
        )
    assert raised.value is failure
    assert owner.process is not None
    assert signalled == [owner.process.pid]
    assert owner.process.returncode == SAMPLE_CHILD_EXIT_CODE
    assert owner.rollback_confirmed and owner.producer_quiescent
    assert not owner.termination_requested
    assert owner.process.stdout.closed and owner.process.stderr.closed


def test_bind_collision_detection_reassembles_same_stream_chunks_across_interleaving():
    output = [
        remote_helper._CapturedFragment("stderr", "Address already in", False),
        remote_helper._CapturedFragment("stdout", "use", True),
        remote_helper._CapturedFragment("stderr", " use", True),
    ]

    assert remote_helper.is_bind_collision(output)


def test_bind_collision_detection_does_not_cross_lines_or_streams():
    assert not remote_helper.is_bind_collision(
        [
            remote_helper._CapturedFragment("stderr", "Address already in", True),
            remote_helper._CapturedFragment("stderr", " use", True),
        ]
    )
    assert not remote_helper.is_bind_collision(
        [
            remote_helper._CapturedFragment("stderr", "Address already in", False),
            remote_helper._CapturedFragment("stdout", " use", True),
        ]
    )


@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_address_probe_finalizer_failure_rolls_back_every_socket(monkeypatch, failure_type):
    original_socket = socket.socket
    sockets = []
    failure = failure_type("address probe close interrupted adoption")

    def acquire(*args, **kwargs):
        owned = original_socket(*args, **kwargs)
        sockets.append(owned)
        controlled = create_autospec(original_socket, instance=True, spec_set=True)
        controlled.bind.side_effect = owned.bind
        if len(sockets) == 2:

            def close_then_fail():
                owned.close()
                raise failure

            controlled.close.side_effect = close_then_fail
        else:
            controlled.close.side_effect = owned.close
        return controlled

    monkeypatch.setattr(remote_helper.socket, "socket", acquire)
    try:
        with pytest.raises(failure_type) as raised:
            remote_helper.allocate_service_address((0, 0))
        assert raised.value is failure
        assert len(sockets) == 3
        assert all(owned.fileno() == -1 for owned in sockets)
    finally:
        for owned in sockets:
            owned.close()


def test_preferred_address_is_leased_before_random_allocation(monkeypatch):
    def unexpected_random():
        raise AssertionError("a free preferred address should be reused")

    monkeypatch.setattr(remote_helper, "random_address", unexpected_random)
    allocated = remote_helper.allocate_service_address((), preferred_address="127.64.0.7")
    try:
        assert allocated == "127.64.0.7"
    finally:
        allocated.lease.close()


def test_preferred_address_held_by_another_session_falls_back(monkeypatch):
    first = remote_helper.allocate_service_address((), preferred_address="127.64.0.7")
    monkeypatch.setattr(remote_helper, "random_address", lambda: "127.64.0.8")
    try:
        second = remote_helper.allocate_service_address((), preferred_address="127.64.0.7")
        try:
            assert second == "127.64.0.8"
        finally:
            second.lease.close()
    finally:
        first.lease.close()


def test_preferred_address_with_occupied_service_port_falls_back(monkeypatch):
    monkeypatch.setattr(remote_helper, "random_address", lambda: "127.64.0.8")
    with socket.socket() as listener:
        listener.bind(("127.64.0.7", 0))
        port = listener.getsockname()[1]
        allocated = remote_helper.allocate_service_address((port,), preferred_address="127.64.0.7")
        try:
            assert allocated == "127.64.0.8"
            # Rejected candidates must release their address lease.
            reused = remote_helper.allocate_service_address((), preferred_address="127.64.0.7")
            reused.lease.close()
        finally:
            allocated.lease.close()


@pytest.mark.parametrize("address", (None, "127.64.0.7"))
def test_decode_start_accepts_address_preference(start_command, address):
    start_command["preferred_address"] = address
    assert remote_helper.decode_command(start_command).preferred_address == address


def test_decode_start_requires_address_preference_field(start_command):
    del start_command["preferred_address"]
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)


@pytest.mark.parametrize(
    "address", (False, 1, [], "", "127.0.0.1", "127.64.0.0", "127.127.255.255", "::1")
)
def test_decode_start_rejects_invalid_address_preference(start_command, address):
    start_command["preferred_address"] = address
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)


@pytest.mark.parametrize("termination", ("exit", "kill"))
def test_address_lease_coordinates_helpers_and_releases_on_exit(tmp_path, monkeypatch, termination):
    address = "127.64.0.3"
    other_address = "127.64.0.4"
    script = """
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("remote_helper", sys.argv[1])
remote_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote_helper)
remote_helper.workspace_root = lambda: Path(sys.argv[2])
remote_helper.random_address = lambda: sys.argv[3]
allocated = remote_helper.allocate_service_address(())
print(allocated, flush=True)
sys.stdin.buffer.read()
"""
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path / "local-user")
    candidates = iter((address, other_address, address))
    monkeypatch.setattr(remote_helper, "random_address", lambda: next(candidates))
    with remote_helper.subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(ROOT / "python/zephyr_remote_openocd/remote_helper.py"),
            str(tmp_path / "other-user"),
            address,
        ],
        stdin=remote_helper.subprocess.PIPE,
        stdout=remote_helper.subprocess.PIPE,
        stderr=remote_helper.subprocess.PIPE,
    ) as process:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            assert select.select([process.stdout], [], [], 30)[0]
            assert process.stdout.readline() == f"{address}\n".encode()
            allocated = remote_helper.allocate_service_address(())
            try:
                assert allocated == other_address
            finally:
                allocated.lease.close()
            if termination == "kill":
                process.kill()
            else:
                process.stdin.close()
            expected_status = -signal.SIGKILL if termination == "kill" else 0
            assert process.wait(timeout=30) == expected_status
            reused = remote_helper.allocate_service_address(())
            try:
                assert reused == address
            finally:
                reused.lease.close()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=30)


@pytest.mark.parametrize("storage", ("runtime", "cache"))
def test_workspace_storage_isolates_legacy_reclamation(tmp_path, monkeypatch, storage):
    monkeypatch.setattr(remote_helper.Path, "home", lambda: tmp_path / "home")
    if storage == "runtime":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        legacy_root = tmp_path / "zephyr_remote_openocd"
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        legacy_root = tmp_path / "home/.cache/zephyr_remote_openocd/sessions"
    legacy = legacy_root / "abandoned"
    (legacy / "staged").mkdir(parents=True)
    (legacy / remote_helper.SESSION_LOCK).touch()
    inputs = legacy / "staged/child-input"
    inputs.write_bytes(b"legacy child may still use this")
    os.utime(legacy, (1.0, 1.0))

    _session_id, workspace, owner_lock = remote_helper.new_workspace()
    try:
        root = workspace.parent
        # Old reclaimers scan arbitrary directories beneath their own root.
        assert root != legacy_root
        assert legacy_root not in root.parents
        assert root not in legacy_root.parents
        assert tuple(legacy_root.iterdir()) == (legacy,)
        assert inputs.read_bytes() == b"legacy child may still use this"
        with pytest.raises(ValueError):
            remote_helper.stage(legacy)
    finally:
        owner_lock.close()
        remote_helper.remove_workspace(workspace)


def test_new_workspace_reclaims_only_unlocked_stale_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    _stale_id, stale, stale_lock = remote_helper.new_workspace()
    _active_id, active, active_lock = remote_helper.new_workspace()
    stale_lock.close()
    old = 1.0
    os.utime(stale, (old, old))
    os.utime(active, (old, old))

    _new_id, new, new_lock = remote_helper.new_workspace()
    try:
        assert not stale.exists()
        assert active.exists()
        assert new.exists()
    finally:
        active_lock.close()
        new_lock.close()


def test_reclaimer_removes_stale_directory_without_lock(tmp_path):
    abandoned = tmp_path / "abandoned"
    abandoned.mkdir()
    os.utime(abandoned, (1.0, 1.0))

    remote_helper.reclaim_stale_workspaces(tmp_path, now=remote_helper.STALE_SESSION_AGE + 2)

    assert not abandoned.exists()


def test_reclaimer_continues_after_stale_lock_error(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / remote_helper.SESSION_LOCK).touch()
    removable = tmp_path / "removable"
    removable.mkdir()
    (removable / remote_helper.SESSION_LOCK).touch()
    old = 1.0
    os.utime(blocked, (old, old))
    os.utime(removable, (old, old))

    original_flock = remote_helper.fcntl.flock

    def fail_blocked_lock(stream, operation):
        if (
            Path(stream.name).name == remote_helper.SESSION_LOCK
            and Path(stream.name).parent.name == blocked.name
        ):
            raise PermissionError("injected stale-lock failure")
        return original_flock(stream, operation)

    monkeypatch.setattr(remote_helper.fcntl, "flock", fail_blocked_lock)
    remote_helper.reclaim_stale_workspaces(tmp_path, now=remote_helper.STALE_SESSION_AGE + 2)

    assert blocked.exists()
    assert not removable.exists()


@pytest.mark.parametrize("source", ("marker", "lock", "metadata-workspace"))
@pytest.mark.parametrize("error_number", (errno.EACCES, errno.EIO))
def test_reclaimer_retains_entries_when_inspection_fails(
    tmp_path, monkeypatch, source, error_number
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    _session_id, workspace, owner_lock = remote_helper.new_workspace()
    owner_lock.close()
    marker = workspace / remote_helper.UNCONFIRMED_CHILD
    if source == "marker":
        marker.touch()
        uncertain = marker
    elif source == "lock":
        uncertain = workspace / remote_helper.SESSION_LOCK
    else:
        uncertain = workspace
    metadata = (remote_helper._lease_path(workspace), remote_helper._closure_path(workspace))
    metadata[1].touch()
    inputs = workspace / "staged/child-input"
    inputs.write_bytes(b"retain uncertain inputs")
    for path in (workspace, *metadata):
        os.utime(path, (1.0, 1.0))

    original_stat = os.stat
    original_lstat = os.lstat

    def inspect(operation, path, *args, **kwargs):
        if path == uncertain or path == str(uncertain):
            raise OSError(error_number, "injected filesystem inspection failure")
        return operation(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            remote_helper.os,
            "stat",
            lambda *args, **kwargs: inspect(original_stat, *args, **kwargs),
        )
        patch.setattr(
            remote_helper.os,
            "lstat",
            lambda *args, **kwargs: inspect(original_lstat, *args, **kwargs),
        )
        remote_helper.reclaim_stale_workspaces(tmp_path, now=remote_helper.STALE_SESSION_AGE + 2)

    assert inputs.read_bytes() == b"retain uncertain inputs"
    assert all(path.is_file() for path in metadata)
    if source == "marker":
        assert marker.is_file()


def test_reclaimer_inspects_residual_marker_after_session_lock_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    _session_id, workspace, owner_lock = remote_helper.new_workspace()
    marker = workspace / remote_helper.UNCONFIRMED_CHILD
    os.utime(workspace, (1.0, 1.0))
    original_flock = remote_helper.fcntl.flock

    def finish_owner_before_lock_acquisition(stream, operation):
        if Path(stream.name) == workspace / remote_helper.SESSION_LOCK:
            marker.touch()
            owner_lock.close()
        return original_flock(stream, operation)

    monkeypatch.setattr(remote_helper.fcntl, "flock", finish_owner_before_lock_acquisition)
    try:
        remote_helper.reclaim_stale_workspaces(tmp_path, now=remote_helper.STALE_SESSION_AGE + 2)
        assert owner_lock.closed
        assert marker.is_file()
        assert (workspace / "staged").is_dir()
    finally:
        owner_lock.close()


def test_new_workspace_removes_partial_directory_on_initialization_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    path_type = type(tmp_path)
    original_mkdir = path_type.mkdir
    failure = OSError("injected staging-directory failure")

    def fail_staging_directory(path, *args, **kwargs):
        if path.name == "staged":
            raise failure
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "mkdir", fail_staging_directory)

    with pytest.raises(OSError) as raised:
        remote_helper.new_workspace()

    assert raised.value is failure
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("boundary", ("constructor", "loop"))
def test_coordinator_adoption_failure_attempts_lock_cleanup_after_workspace_failure(
    tmp_path, monkeypatch, boundary
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    allocate = remote_helper.new_workspace
    remove = remote_helper.remove_workspace
    acquired = []
    failure = RuntimeError("coordinator construction failed")
    cleanup_failure = OSError("workspace cleanup failed")
    cleanup_detail = "workspace lease cleanup also failed"
    cleanup_failure.add_note(cleanup_detail)

    def new_workspace():
        resources = allocate()
        acquired.append(resources)
        return resources

    def fail_remove(_workspace):
        raise cleanup_failure

    monkeypatch.setattr(remote_helper, "new_workspace", new_workspace)
    monkeypatch.setattr(remote_helper, "remove_workspace", fail_remove)
    if boundary == "constructor":
        monkeypatch.setattr(
            remote_helper.asyncio, "Queue", create_autospec(asyncio.Queue, side_effect=failure)
        )
    else:
        monkeypatch.setattr(
            remote_helper.asyncio.events,
            "new_event_loop",
            create_autospec(asyncio.events.new_event_loop, side_effect=failure),
        )
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        with pytest.raises(SystemExit) as raised:
            remote_helper.control()
        assert (raised.value.__cause__ or raised.value) is failure
        assert acquired[0][2].closed
        assert any(str(cleanup_failure) in note for note in failure.__notes__)
        assert any(cleanup_detail in note for note in failure.__notes__)
        assert all(
            signal.getsignal(signum) == handler for signum, handler in previous_handlers.items()
        )
    finally:
        if acquired:
            acquired[0][2].close()
            remove(acquired[0][1])


@pytest.mark.parametrize("boundary", ("reader", "writer"))
@pytest.mark.parametrize("rollback_fails", (False, True))
def test_nonblocking_acquisition_rolls_back_effect_before_failed_adoption(
    monkeypatch, boundary, rollback_fails
):
    read_fd, write_fd = os.pipe()
    original_set_blocking = os.set_blocking
    primary = KeyboardInterrupt("descriptor adoption interrupted")
    secondary = OSError("descriptor rollback failed")
    secondary.add_note("rollback retained nested detail")

    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "w", encoding="utf-8") as stdout:
        descriptor = read_fd if boundary == "reader" else write_fd

        def set_blocking(target, blocking):
            original_set_blocking(target, blocking)
            if target == descriptor:
                if not blocking:
                    raise primary
                if rollback_fails:
                    raise secondary

        monkeypatch.setattr(remote_helper.sys, "stdout", stdout)
        monkeypatch.setattr(remote_helper.os, "set_blocking", set_blocking)
        with pytest.raises(KeyboardInterrupt) as raised:
            if boundary == "reader":
                remote_helper._AsyncInput(descriptor)
            else:
                remote_helper._ProtocolOutput()
        assert raised.value is primary
        assert os.get_blocking(descriptor)
        if rollback_fails:
            assert any(str(secondary) in note for note in primary.__notes__)
            assert any(secondary.__notes__[0] in note for note in primary.__notes__)


def test_protocol_writer_close_releases_pending_buffer_after_interruption(monkeypatch):
    read_fd, write_fd = os.pipe()
    original_set_blocking = os.set_blocking
    failure = KeyboardInterrupt("writer mode restoration interrupted")
    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "w", encoding="utf-8") as stdout:
        monkeypatch.setattr(remote_helper.sys, "stdout", stdout)
        output = remote_helper._ProtocolOutput()
        output.enqueue(b"queued frame\n")

        def set_blocking(descriptor, blocking):
            original_set_blocking(descriptor, blocking)
            if descriptor == write_fd and blocking:
                raise failure

        monkeypatch.setattr(remote_helper.os, "set_blocking", set_blocking)
        with pytest.raises(KeyboardInterrupt) as raised:
            output.close()
        assert raised.value is failure
        assert not output.frames
        assert output.pending_bytes == 0
        assert os.get_blocking(write_fd)


def test_workspace_allocation_rollback_preserves_primary_and_cleans_independently(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    original_mkdir = Path.mkdir
    original_open = Path.open
    locks = []
    primary = OSError("staged directory creation failed")
    secondary = KeyboardInterrupt("allocation lock close interrupted")
    secondary.add_note("lock rollback retained detail")

    def mkdir(path, *args, **kwargs):
        if path.name == "staged":
            raise primary
        return original_mkdir(path, *args, **kwargs)

    def open_path(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path.name == remote_helper.SESSION_LOCK:
            locks.append(stream)
            close = stream.close

            def close_then_interrupt():
                close()
                raise secondary

            monkeypatch.setattr(stream, "close", close_then_interrupt)
        return stream

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(BaseException) as raised:
        remote_helper.new_workspace()
    assert raised.value is primary
    assert locks and all(lock.closed for lock in locks)
    assert not tuple(tmp_path.iterdir())
    assert any(str(secondary) in note for note in primary.__notes__)
    assert any(secondary.__notes__[0] in note for note in primary.__notes__)


def _start_session_command(argv, sentinels=()):
    return (
        json.dumps(
            {
                "version": 2,
                "type": "START",
                "completion_policy": "live_server",
                "argv": argv,
                "environment": {},
                "required_paths": [],
                "services": [],
                "required_output_sentinels": list(sentinels),
                "readiness_timeout": 30,
                "literal_prefix": len(argv),
                "argv_templates": [],
                "preferred_address": None,
            }
        ).encode()
        + b"\n"
    )


@pytest.mark.parametrize("source", ("available-prefix", "continuous"))
def test_final_reader_scan_consumes_available_prefix_with_finite_budget(monkeypatch, source):
    read_fd, write_fd = os.pipe()
    original_read = os.read
    prefix = b"prefix before READY"

    def read(descriptor, size):
        if descriptor != read_fd:
            return original_read(descriptor, size)
        if source == "continuous":
            return b"x" * size
        return original_read(descriptor, min(size, 3))

    monkeypatch.setattr(remote_helper.os, "read", read)

    async def run():
        reader = remote_helper._AsyncInput(read_fd)
        next_read = None
        try:
            os.write(write_fd, prefix)
            checkpoint = reader.checkpoint()
            captured = bytearray(await reader.read())
            assert not checkpoint.done()
            if source == "continuous":
                while not checkpoint.done():
                    captured.extend(await reader.read())
                assert len(captured) == remote_helper.MAX_FINAL_OBSERVATION_BYTES
            else:
                while len(captured) < len(prefix):
                    captured.extend(await reader.read())
                assert bytes(captured) == prefix
                next_read = asyncio.create_task(reader.read())
                await checkpoint
                assert not next_read.done()
        finally:
            if next_read is not None:
                next_read.cancel()
                with suppress(asyncio.CancelledError):
                    await next_read
            reader.close()

    try:
        asyncio.run(run())
        assert os.get_blocking(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_supervised_child_terminates_descendant_after_leader_term(tmp_path):
    descendant_path = tmp_path / "descendant.pid"
    child = remote_helper._spawn_child(
        (sys.executable, "-c", _forking_child_code(True), str(descendant_path))
    )
    descendant_pid = None
    descendant_pidfd = None
    try:
        descendant_pid = _wait_for_descendant(descendant_path)
        descendant_pidfd = os.pidfd_open(descendant_pid)
        assert os.getpgid(descendant_pid) == child.pid
        assert child.poll() is None

        asyncio.run(child.terminate())
        child.close_streams()

        assert child.returncode == 0
        assert child.termination_requested
        assert child.group_disposed
        _assert_pidfd_exited(descendant_pidfd)
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, 0)
    finally:
        _cleanup_test_child(child, descendant_pidfd)
        if descendant_pidfd is not None:
            os.close(descendant_pidfd)


def test_supervised_child_warns_and_terminates_descendant_after_leader_exit(tmp_path, capfd):
    descendant_path = tmp_path / "descendant.pid"
    child = remote_helper._spawn_child(
        (sys.executable, "-c", _forking_child_code(False), str(descendant_path))
    )
    descendant_pid = None
    descendant_pidfd = None
    leader_pidfd = None
    try:
        descendant_pid = _wait_for_descendant(descendant_path)
        descendant_pidfd = os.pidfd_open(descendant_pid)
        leader_pidfd = os.pidfd_open(child.pid)
        assert os.getpgid(descendant_pid) == child.pid

        os.kill(child.pid, signal.SIGUSR1)
        _assert_pidfd_exited(leader_pidfd)
        assert child.process.returncode is None

        asyncio.run(child.terminate())
        child.close_streams()

        assert child.returncode == 0
        assert not child.termination_requested
        assert child.group_disposed
        _assert_pidfd_exited(descendant_pidfd)
        assert str(descendant_pid) in capfd.readouterr().err
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, 0)
    finally:
        _cleanup_test_child(child, descendant_pidfd)
        if descendant_pidfd is not None:
            os.close(descendant_pidfd)
        if leader_pidfd is not None:
            os.close(leader_pidfd)


def test_supervised_child_skips_kill_after_group_disappears(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self.wait_calls = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = 0
            return self.returncode

    process = Process()
    signals = []

    def killpg(_pid, signum):
        signals.append(signum)
        if signum == 0:
            raise ProcessLookupError

    monkeypatch.setattr(remote_helper.os, "killpg", killpg)
    child = remote_helper.SupervisedChild(process)
    child._observed_returncode = 0

    asyncio.run(child.terminate())

    assert signal.SIGTERM in signals
    assert signal.SIGKILL not in signals
    assert process.wait_calls
    assert all(timeout is not None for timeout in process.wait_calls)


def test_supervised_child_reaps_and_cleans_up_after_signal_errors(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self.wait_calls = []

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            self.returncode = 0
            return self.returncode

    process = Process()
    child = remote_helper.SupervisedChild(process)
    child._observed_returncode = 0
    term_error = RuntimeError("term failed")

    def fail_term(_pid, signum):
        if signum == signal.SIGTERM:
            raise term_error
        raise ProcessLookupError

    monkeypatch.setattr(remote_helper.os, "killpg", fail_term)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(child.terminate())
        child.close_streams()

    assert raised.value is term_error
    assert process.wait_calls
    assert all(timeout is not None for timeout in process.wait_calls)


def test_supervised_child_group_cleanup_ignores_diagnostic_failure(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def wait(self, timeout=None):
            self.returncode = -signal.SIGKILL
            return self.returncode

    child = remote_helper.SupervisedChild(Process())
    monkeypatch.setattr(remote_helper.os, "waitid", lambda *_args: None)
    signals = []

    def killpg(_pid, signum):
        signals.append(signum)
        if signum == 0 and child.process.returncode is not None:
            raise ProcessLookupError

    monkeypatch.setattr(remote_helper.os, "killpg", killpg)

    async def leader_exited():
        return True

    monkeypatch.setattr(child, "_wait_for_leader_exit", leader_exited)
    monkeypatch.setattr(
        child,
        "_remaining_group_members",
        lambda: (_ for _ in ()).throw(OSError("proc unavailable")),
    )

    asyncio.run(child.terminate())

    assert signals.count(signal.SIGTERM) == 1
    assert signals.count(signal.SIGKILL) == 1
    assert signals.index(signal.SIGTERM) < signals.index(signal.SIGKILL)


@pytest.fixture
def cleanup_process(monkeypatch):
    process = create_autospec(subprocess.Popen, instance=True)
    process.pid = 123
    process.returncode = None
    process.stdout = io.BytesIO()
    process.stderr = io.BytesIO()
    waitid = os.waitid

    def observe_owned_process(kind, pid, options):
        if kind == os.P_PID and pid == process.pid:
            return None
        return waitid(kind, pid, options)

    monkeypatch.setattr(remote_helper.os, "waitid", observe_owned_process)

    def reap(timeout=None):
        assert timeout is not None and math.isfinite(timeout) and timeout > 0
        process.returncode = -signal.SIGKILL
        return process.returncode

    process.wait.side_effect = reap
    return process


def _run_group_cleanup(process, mode):
    if mode == "rollback":
        remote_helper._rollback_spawned_process(process)
    else:
        child = remote_helper.SupervisedChild(process)
        child._observed_returncode = 0
        try:
            asyncio.run(child.terminate())
        finally:
            child.close_streams()


@pytest.mark.parametrize("mode", ("supervised", "rollback"))
@pytest.mark.parametrize("observation", ("exists", "late-exit", "permission-denied"))
def test_group_cleanup_fails_when_disappearance_is_unconfirmed(
    monkeypatch, cleanup_process, mode, observation
):
    now = 0.0
    waits = []
    group_gone = False

    def advance(delay):
        nonlocal now, group_gone
        assert math.isfinite(delay) and delay > 0
        waits.append(delay)
        if observation == "late-exit":
            # The final member exits, but observation resumes after expiry.
            now += remote_helper.CHILD_GROUP_EXIT_TIMEOUT + delay
            group_gone = True
        else:
            now += delay

    async def async_advance(delay):
        advance(delay)

    def killpg(pid, signum):
        assert pid == cleanup_process.pid
        if cleanup_process.returncode is not None:
            # No further signalling is safe once reaping releases the PID.
            assert signum == 0
            if observation == "permission-denied":
                raise PermissionError("group cannot be inspected")
            if group_gone:
                raise ProcessLookupError

    monkeypatch.setattr(remote_helper.os, "killpg", killpg)
    monkeypatch.setattr(
        remote_helper, "time", SimpleNamespace(monotonic=lambda: now, sleep=advance)
    )
    monkeypatch.setattr(remote_helper.asyncio, "sleep", async_advance)

    expected = PermissionError if observation == "permission-denied" else TimeoutError
    with pytest.raises(expected):
        _run_group_cleanup(cleanup_process, mode)

    assert cleanup_process.returncode is not None
    assert cleanup_process.stdout.closed and cleanup_process.stderr.closed
    if observation == "exists":
        assert waits
        assert now <= remote_helper.CHILD_GROUP_EXIT_TIMEOUT


@pytest.mark.parametrize("mode", ("supervised", "rollback"))
def test_group_cleanup_waits_for_disappearance_after_reaping(monkeypatch, cleanup_process, mode):
    group_gone = False
    now = 0.0

    def finish_group_exit(delay):
        nonlocal group_gone, now
        assert cleanup_process.returncode is not None
        assert math.isfinite(delay) and delay > 0
        # Model the last member being reaped during the observation wait.
        group_gone = True
        now += delay

    async def async_finish_group_exit(delay):
        finish_group_exit(delay)

    def killpg(pid, signum):
        assert pid == cleanup_process.pid
        if cleanup_process.returncode is not None:
            assert signum == 0
        if group_gone:
            raise ProcessLookupError

    monkeypatch.setattr(remote_helper.os, "killpg", killpg)
    monkeypatch.setattr(
        remote_helper, "time", SimpleNamespace(monotonic=lambda: now, sleep=finish_group_exit)
    )
    monkeypatch.setattr(remote_helper.asyncio, "sleep", async_finish_group_exit)

    _run_group_cleanup(cleanup_process, mode)

    assert group_gone
    assert cleanup_process.returncode is not None


def test_supervised_child_cleanup_uses_finite_budgets_after_failures(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self.wait_calls = []

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            raise TimeoutError("reap failed")

    process = Process()
    child = remote_helper.SupervisedChild(process)
    monkeypatch.setattr(remote_helper.os, "waitid", lambda *_args: None)
    signal_error = RuntimeError("signal failed")
    signals = []

    def fail_signal(_pid, signum):
        signals.append(signum)
        if signum != 0:
            raise signal_error

    async def fail_leader_wait():
        raise RuntimeError("leader wait failed")

    now = 0.0

    async def advance(delay):
        nonlocal now
        assert math.isfinite(delay) and delay > 0
        now += delay

    monkeypatch.setattr(remote_helper, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(remote_helper.asyncio, "sleep", advance)
    monkeypatch.setattr(remote_helper.os, "killpg", fail_signal)
    monkeypatch.setattr(child, "_wait_for_leader_exit", fail_leader_wait)
    monkeypatch.setattr(child, "_warn_remaining_group_members", lambda: None)

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(child.terminate())

    assert raised.value is signal_error
    assert signal.SIGTERM in signals and signal.SIGKILL in signals
    assert signals.index(signal.SIGTERM) < signals.index(signal.SIGKILL)
    assert all(
        timeout is not None
        and math.isfinite(timeout)
        and 0 < timeout <= remote_helper.CHILD_REAP_TIMEOUT
        for timeout in process.wait_calls
    )
    assert any("leader wait failed" in note for note in raised.value.__notes__)
    assert any("reap failed" in note for note in raised.value.__notes__)


def test_decode_command_rejects_malformed_required_path_before_launch(start_command):
    start_command["required_paths"] = [{"kind": "socket", "path": "not-valid"}]
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)


def test_decode_command_rejects_previous_textual_expansion_contract(start_command):
    del start_command["argv_templates"]
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)


@pytest.mark.parametrize(
    "templates",
    (
        None,
        [{"index": True, "parts": ["text"]}],
        [{"index": 0, "parts": ["cannot override configured prefix"]}],
        [{"index": 2, "parts": ["outside argv"]}],
        [{"index": 1, "parts": []}],
        [{"index": 1, "parts": ["nul\0"]}],
        [{"index": 1, "parts": [{"session": "unknown"}]}],
        [{"index": 1, "parts": [{"session": "address", "extra": True}]}],
        [{"index": 1, "parts": [{"tcl_word": [{"tcl_word": ["nested"]}]}]}],
        [{"index": 1, "parts": ["first"]}, {"index": 1, "parts": ["duplicate"]}],
    ),
)
def test_decode_command_rejects_malformed_owned_substitutions(start_command, templates):
    start_command["argv_templates"] = templates
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)


def test_decode_command_rejects_tcl_quoting_in_a_filesystem_path(start_command):
    start_command["required_paths"] = [
        {"kind": "file", "path": {"parts": [{"tcl_word": ["not a path template"]}]}}
    ]
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        pytest.param("argv", [], id="empty-argv"),
        pytest.param("environment", {"BAD=NAME": "value"}, id="environment-name"),
        pytest.param("required_output_sentinels", [" READY "], id="untrimmed-marker"),
        pytest.param("required_output_sentinels", ["READY", "READY"], id="duplicate-marker"),
        pytest.param("readiness_timeout", 0, id="zero-timeout"),
        pytest.param("literal_prefix", 3, id="prefix-outside-argv"),
    ),
)
def test_decode_command_rejects_invalid_start_values(start_command, field, value):
    start_command[field] = value
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)


@pytest.mark.parametrize(
    "services",
    (
        pytest.param(
            [
                {"name": "gdb", "remote_port": 3333},
                {"name": "gdb", "remote_port": 6333},
            ],
            id="duplicate-names",
        ),
        pytest.param(
            [
                {"name": "gdb", "remote_port": 3333},
                {"name": "tcl", "remote_port": 3333},
            ],
            id="duplicate-ports",
        ),
    ),
)
def test_decode_command_rejects_duplicate_services(start_command, services):
    start_command["services"] = services
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)


def test_decode_command_rejects_unknown_start_and_stop_fields(start_command):
    start_command["future"] = True
    with pytest.raises(ValueError):
        remote_helper.decode_command(start_command)
    with pytest.raises(ValueError):
        remote_helper.decode_command({"version": 2, "type": "STOP", "future": True})


@pytest.mark.parametrize("suffix", ("lease", "closed"))
def test_reclaimer_removes_orphaned_lease_but_preserves_live_workspace(
    tmp_path, monkeypatch, suffix
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    _session_id, workspace, owner_lock = remote_helper.new_workspace()
    try:
        with remote_helper._stage_lease(workspace):
            orphan = tmp_path / f".removed.{suffix}"
            orphan.write_bytes(b"closed")
            live_lease = remote_helper._lease_path(workspace)
            for path in (orphan, live_lease):
                os.utime(path, (1.0, 1.0))
            remote_helper.reclaim_stale_workspaces(
                tmp_path, now=remote_helper.STALE_SESSION_AGE + 2
            )
            assert not orphan.exists()
            assert live_lease.exists()
            assert workspace.is_dir()
    finally:
        owner_lock.close()


@pytest.fixture
def coordinated_helper(tmp_path, monkeypatch, control_pipe):
    """Use real workspace, child owners, pipes, and the lifecycle coordinator."""
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session = remote_helper.ControlSession.create()
    _, writer = control_pipe
    events = []
    children = []
    spawn = remote_helper._spawn_child

    def acquire(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(remote_helper, "_spawn_child", acquire)
    monkeypatch.setattr(remote_helper, "emit", lambda kind, **fields: events.append((kind, fields)))
    yield session, writer, events, children
    for child in children:
        _cleanup_test_child(child, None)
    session._release_workspace()


def _terminal_event(events):
    terminals = [fields for kind, fields in events if kind == "SESSION_ENDED"]
    assert len(terminals) == 1
    return decode_terminal(
        json.loads(json.dumps(dict(version=2, type="SESSION_ENDED", **terminals[0])))
    )


def _run_terminated(session, *, fails):
    if fails:
        with pytest.raises(SystemExit) as raised:
            session.run()
        assert raised.value.code == 1
    else:
        session.run()
    assert isinstance(session.lifecycle.state, Closed)


@pytest.mark.parametrize("suffix", (b"\n", b" ", b'{"version":2,"type":"STOP"}\n', b"junk"))
def test_post_start_bytes_are_failure_even_in_same_read(coordinated_helper, suffix):
    session, writer, events, _children = coordinated_helper
    stream = io.BytesIO()
    write_start(stream, RemoteProcess((sys.executable, "-c", "import signal;signal.pause()")), ())
    writer.write(stream.getvalue() + suffix)
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.trigger == Trigger.PROTOCOL_FAILURE
    assert snapshot.outcome.primary_failure is not None
    assert snapshot.cleanup.confirmed
    assert not _children  # Reject the batched invalid input before physical entry.


@pytest.mark.parametrize("partial", (False, True))
def test_controller_eof_before_start_has_explicit_result(coordinated_helper, partial):
    session, writer, events, _children = coordinated_helper
    if partial:
        writer.write(b'{"version":2')
    writer.close()
    _run_terminated(session, fails=partial)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.trigger == (
        Trigger.PROTOCOL_FAILURE if partial else Trigger.CONTROLLER_EOF
    )
    assert snapshot.outcome.child_result is None
    assert snapshot.cleanup.confirmed


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_missing_required_path_fails_before_attempt_admission(coordinated_helper, kind):
    session, writer, events, children = coordinated_helper
    missing = session.work / "staged" / "missing"
    write_start(
        writer,
        RemoteProcess((sys.executable,), required_paths=(RemotePathCheck(str(missing), kind),)),
        (),
    )
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.primary_failure is not None
    assert str(missing) in snapshot.outcome.primary_failure.message
    assert not children and not any(event == "ATTEMPT" for event, _ in events)
    assert snapshot.cleanup.confirmed


@pytest.mark.parametrize("policy", tuple(CompletionPolicy))
def test_completion_policy_controls_readiness(coordinated_helper, monkeypatch, policy):
    session, writer, events, children = coordinated_helper
    record = remote_helper.emit

    def emit(kind, **fields):
        record(kind, **fields)
        if kind == "READY":
            writer.close()

    monkeypatch.setattr(remote_helper, "emit", emit)
    code = (
        "print('done',flush=True)"
        if policy == CompletionPolicy.PROCESS_EXIT
        else "import signal;print('ready',flush=True);signal.pause()"
    )
    write_start(writer, RemoteProcess((sys.executable, "-c", code), completion_policy=policy), ())
    _run_terminated(session, fails=False)
    kinds = [kind for kind, _ in events]
    assert ("READY" in kinds) == (policy == CompletionPolicy.LIVE_SERVER)
    assert kinds.index("ATTEMPT") < kinds.index("SESSION_ENDED")
    snapshot = _terminal_event(events)
    assert snapshot.cleanup.confirmed
    assert snapshot.outcome.child_result is not None
    assert snapshot.outcome.child_result.termination_requested == (
        policy == CompletionPolicy.LIVE_SERVER
    )
    assert all(child.process.stdout.closed and child.process.stderr.closed for child in children)
    assert not session.work.exists() and session.workspace_lock.closed


@pytest.mark.parametrize("admission", ("ATTEMPT", "READY", "SESSION_ENDED"))
def test_admission_failure_does_not_publish_success_or_repeat_terminal(
    coordinated_helper, monkeypatch, admission
):
    session, writer, events, children = coordinated_helper
    record = remote_helper.emit
    attempts = []

    def emit(kind, **fields):
        attempts.append(kind)
        if kind == admission:
            if kind == "SESSION_ENDED":
                assert not session.work.exists()
            raise BrokenPipeError("admission failed")
        record(kind, **fields)
        if kind == "READY":
            writer.close()

    monkeypatch.setattr(remote_helper, "emit", emit)
    write_start(writer, RemoteProcess((sys.executable, "-c", "import signal;signal.pause()")), ())
    _run_terminated(session, fails=True)
    assert attempts.count("SESSION_ENDED") == 1
    assert len(children) == (0 if admission == "ATTEMPT" else 1)
    assert session.lifecycle.state.snapshot.cleanup.confirmed
    assert not session.work.exists()
    if admission == "SESSION_ENDED":
        assert session.lifecycle.state.local_diagnostics
    else:
        assert _terminal_event(events).outcome.primary_failure.code == admission + "_ADMISSION"


@pytest.mark.parametrize("boundary", ("before_spawn", "returned", "wrapper_failure"))
def test_signal_capture_and_spawn_handoff_keep_cleanup_reachable(
    coordinated_helper, monkeypatch, boundary
):
    session, writer, events, _children = coordinated_helper
    spawn = remote_helper._spawn_child
    supervisor = remote_helper.SupervisedChild

    def acquire(*args, **kwargs):
        if boundary == "before_spawn":
            session.handle_signal(signal.SIGTERM)
        child = spawn(*args, **kwargs)
        if boundary == "returned":
            session.handle_signal(signal.SIGINT)
            raise KeyboardInterrupt("interrupted after return")
        return child

    def construct(*args, **kwargs):
        session.handle_signal(signal.SIGTERM)
        raise RuntimeError("supervisor adoption failed")

    monkeypatch.setattr(remote_helper, "_spawn_child", acquire)
    if boundary == "wrapper_failure":
        monkeypatch.setattr(remote_helper, "SupervisedChild", construct)
    write_start(writer, RemoteProcess((sys.executable, "-c", "import signal;signal.pause()")), ())
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.primary_failure is not None
    assert snapshot.cleanup.confirmed
    assert "READY" not in [kind for kind, _ in events]
    assert not session.work.exists() and session.workspace_lock.closed
    monkeypatch.setattr(remote_helper, "SupervisedChild", supervisor)


@pytest.mark.parametrize("failure", ("observer", "workspace"))
def test_nested_failure_detail_survives_cleanup_and_wire(coordinated_helper, monkeypatch, failure):
    session, writer, events, children = coordinated_helper
    nested = RuntimeError("nested cause")
    nested.add_note("nested retained note")
    grouped = ExceptionGroup(
        "composed failure", [OSError("first cause"), ExceptionGroup("inner", [nested])]
    )
    grouped.add_note("outer retained note")
    if failure == "observer":

        async def fail_output(_child, _name, _stream):
            raise grouped

        monkeypatch.setattr(session, "_observe_output", fail_output)
    else:

        def fail_removal(_work):
            raise grouped

        monkeypatch.setattr(remote_helper, "remove_workspace", fail_removal)
        record = remote_helper.emit

        def emit(kind, **fields):
            record(kind, **fields)
            if kind == "READY":
                writer.close()

        monkeypatch.setattr(remote_helper, "emit", emit)
    write_start(writer, RemoteProcess((sys.executable, "-c", "import signal;signal.pause()")), ())
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    detail = snapshot.outcome.primary_failure
    assert detail is not None
    assert detail.diagnostics[1].diagnostics[0].diagnostics[0].message == "nested retained note"
    assert detail.diagnostics[-1].message == "outer retained note"
    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert session.workspace_lock.closed
    assert snapshot.cleanup.confirmed == (failure == "observer")


@pytest.mark.parametrize("final_observation", ("ready", "exit", "timeout"))
def test_readiness_deadline_processes_final_visible_observation(
    tmp_path, monkeypatch, control_pipe, final_observation
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    children = []
    events = []
    original_spawn = remote_helper._spawn_child
    original_read = remote_helper.os.read
    original_sleep = remote_helper.asyncio.sleep
    output_blocked = True

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    def controlled_read(descriptor, size):
        if output_blocked and children and descriptor == children[0].process.stdout.fileno():
            raise BlockingIOError
        return original_read(descriptor, size)

    async def controlled_sleep(delay):
        nonlocal output_blocked
        if delay == 30:
            # Observe readiness through a duplicate fd without reading any bytes.
            descriptor = os.dup(children[0].process.stdout.fileno())
            try:
                await remote_helper._readable(descriptor)
            finally:
                os.close(descriptor)
            if final_observation == "exit":
                child = children[0]
                descriptor = os.pidfd_open(child.pid)
                try:
                    os.kill(child.pid, signal.SIGUSR1)
                    await remote_helper._readable(descriptor)
                finally:
                    os.close(descriptor)
            # The deadline producer enqueues its fact before yielding again.
            output_blocked = False
        else:
            await original_sleep(delay)

    def emit(kind, **values):
        events.append((kind, values))
        if kind == "READY":
            writer.close()

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper.os, "read", controlled_read)
    monkeypatch.setattr(remote_helper.asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(remote_helper, "emit", emit)
    marker = "diagnostic" if final_observation == "timeout" else "READY"
    writer.write(
        _start_session_command(
            [
                sys.executable,
                "-c",
                "import signal,sys;"
                "signal.signal(signal.SIGUSR1,lambda *_:sys.exit(7));"
                f"print({marker!r},flush=True);signal.pause()",
            ],
            ("READY",),
        )
    )

    _run_terminated(session, fails=final_observation != "ready")
    snapshot = _terminal_event(events)
    assert [kind for kind, _ in events].count("READY") == (1 if final_observation == "ready" else 0)
    assert snapshot.cleanup.confirmed
    if final_observation == "exit":
        assert snapshot.outcome.child_result.returncode == SAMPLE_CHILD_EXIT_CODE
    elif final_observation == "timeout":
        assert snapshot.outcome.primary_failure.code == "READINESS_TIMEOUT"
    assert children[0].process.returncode is not None
    assert not workspace.exists()
    assert lock.closed


@pytest.mark.parametrize("failure_type", (OSError, asyncio.CancelledError))
@pytest.mark.parametrize("restoration_fails", (False, True))
def test_partial_signal_installation_restores_handlers_and_preserves_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    restoration_fails: bool,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = remote_helper.ControlSession.create()
    failure = failure_type("injected signal installation failure")
    restoration_failure = OSError("injected signal restoration failure")
    restoration_detail = "signal restoration retained detail"
    restoration_failure.add_note(restoration_detail)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    original_signal = signal.signal
    installed: set[int] = set()
    restored: list[int] = []

    def install_then_fail(signum: int, handler: SignalHandler) -> SignalHandler:
        result = original_signal(signum, handler)
        if handler == session.handle_signal:
            installed.add(signum)
            if signum == signal.SIGINT:
                raise failure
        elif signum in installed:
            restored.append(signum)
            installed.remove(signum)
            if restoration_fails:
                raise restoration_failure
        return result

    monkeypatch.setattr(remote_helper.signal, "signal", install_then_fail)

    async def run() -> None:
        tasks_before = asyncio.all_tasks()
        output_before = remote_helper._protocol_output.get()
        with pytest.raises(SystemExit):
            await session.run_async()
        assert session.lifecycle.state.snapshot.outcome.primary_failure.message == str(failure)
        assert asyncio.all_tasks() == tasks_before
        assert remote_helper._protocol_output.get() is output_before

    try:
        asyncio.run(run())
        assert set(restored) == set(previous)
        assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())
        if restoration_fails:
            assert restoration_detail in repr(session.lifecycle.state.local_diagnostics)
        assert session.workspace_lock.closed
        assert not session.work.exists()
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        session._release_workspace()


@pytest.mark.parametrize("failure_type", (OSError, KeyboardInterrupt))
def test_signal_restoration_attempts_both_handlers_after_normal_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_pipe: tuple[IO[bytes], IO[bytes]],
    failure_type: type[BaseException],
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = remote_helper.ControlSession.create()
    _reader, writer = control_pipe
    writer.close()
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    original_signal = signal.signal
    failures: dict[int, BaseException] = {
        signum: failure_type(f"restoration failed for {signum}") for signum in previous
    }
    installed: set[int] = set()
    restored: list[int] = []

    def restore_then_fail(signum: int, handler: SignalHandler) -> SignalHandler:
        result = original_signal(signum, handler)
        if handler == session.handle_signal:
            installed.add(signum)
        elif signum in installed:
            restored.append(signum)
            installed.remove(signum)
            raise failures[signum]
        return result

    monkeypatch.setattr(remote_helper.signal, "signal", restore_then_fail)
    try:
        with pytest.raises(SystemExit):
            session.run()
        assert set(restored) == set(previous)
        assert len(session.lifecycle.state.local_diagnostics) == 2
        assert session.lifecycle.state.snapshot.outcome.primary_failure is None
        assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())
        assert session.workspace_lock.closed
        assert not session.work.exists()
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        session._release_workspace()


@pytest.mark.parametrize("primary_type", (None, OSError, asyncio.CancelledError))
@pytest.mark.parametrize("close_type", (OSError, KeyboardInterrupt))
def test_protocol_writer_and_close_failures_preserve_primary_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control_pipe: tuple[IO[bytes], IO[bytes]],
    primary_type: type[BaseException] | None,
    close_type: type[BaseException],
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = remote_helper.ControlSession.create()
    _control_reader, control_writer = control_pipe
    control_writer.close()
    primary = primary_type("injected session setup failure") if primary_type is not None else None
    writer_failure = BrokenPipeError("injected protocol write failure")
    close_failure = close_type("injected protocol descriptor restoration failure")
    close_detail = "protocol descriptor cleanup retained detail"
    close_failure.add_note(close_detail)
    original_signal = signal.signal
    original_write = os.write
    original_set_blocking = os.set_blocking
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    output_read, output_write = os.pipe()
    close_attempts: list[int] = []

    def install_then_fail(signum: int, handler: SignalHandler) -> SignalHandler:
        result = original_signal(signum, handler)
        if signum == signal.SIGINT and handler == session.handle_signal and primary is not None:
            raise primary
        return result

    def fail_write(descriptor: int, payload: Buffer) -> int:
        if descriptor == output_write:
            raise writer_failure
        return original_write(descriptor, payload)

    def restore_then_fail(descriptor: int, blocking: bool) -> None:
        original_set_blocking(descriptor, blocking)
        if descriptor == output_write and blocking:
            close_attempts.append(descriptor)
            raise close_failure

    async def run() -> None:
        tasks_before = asyncio.all_tasks()
        output_before = remote_helper._protocol_output.get()
        with pytest.raises(SystemExit):
            await session.run_async()
        state = session.lifecycle.state
        assert isinstance(state, Closed)
        assert close_detail in repr(state.local_diagnostics)
        all_detail = repr(state.snapshot.outcome) + repr(state.local_diagnostics)
        assert str(writer_failure) in all_detail
        if primary is not None:
            assert state.snapshot.outcome.primary_failure is not None
            assert state.snapshot.outcome.primary_failure.message == str(primary)
        assert asyncio.all_tasks() == tasks_before
        assert remote_helper._protocol_output.get() is output_before

    try:
        with (
            os.fdopen(output_read, "rb"),
            os.fdopen(output_write, "w", encoding="utf-8") as stdout,
            monkeypatch.context() as patch,
        ):
            patch.setattr(remote_helper.sys, "stdout", stdout)
            patch.setattr(remote_helper.signal, "signal", install_then_fail)
            patch.setattr(remote_helper.os, "write", fail_write)
            patch.setattr(remote_helper.os, "set_blocking", restore_then_fail)
            asyncio.run(run())
            assert os.get_blocking(output_write)
        assert close_attempts == [output_write]
        assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())
        assert session.workspace_lock.closed
        assert not session.work.exists()
    finally:
        for signum, handler in previous.items():
            original_signal(signum, handler)
        session._release_workspace()


@pytest.mark.parametrize("cleanup_failure", ("group", "lease"))
def test_unconfirmed_disposal_retains_inputs_but_settles_independent_owners(
    coordinated_helper, monkeypatch, cleanup_failure
):
    session, writer, events, children = coordinated_helper
    record = remote_helper.emit
    terminate = remote_helper.SupervisedChild.terminate
    lease = None

    async def uncertain(child):
        await terminate(child)
        child.group_disposed = False
        raise OSError("group disposal could not be established")

    class UnconfirmedLease:
        def close(self):
            raise OSError("address lease close failed")

    def emit(kind, **fields):
        nonlocal lease
        record(kind, **fields)
        if kind == "READY":
            if cleanup_failure == "lease":
                lease = session._address_lease
                monkeypatch.setattr(session, "_address_lease", UnconfirmedLease())
            writer.close()

    monkeypatch.setattr(remote_helper, "emit", emit)
    if cleanup_failure == "group":
        monkeypatch.setattr(remote_helper.SupervisedChild, "terminate", uncertain)
    write_start(writer, RemoteProcess((sys.executable, "-c", "import signal;signal.pause()")), ())
    try:
        _run_terminated(session, fails=True)
        snapshot = _terminal_event(events)
        assert not snapshot.cleanup.confirmed
        assert (
            "child_group" if cleanup_failure == "group" else "address_lease"
        ) in snapshot.cleanup.residual_resources
        assert "workspace" in snapshot.cleanup.residual_resources
        assert "child_relays" not in snapshot.cleanup.residual_resources
        assert session.work.is_dir()
        assert (session.work / remote_helper.UNCONFIRMED_CHILD).is_file()
        assert session.workspace_lock.closed
        assert children[0].process.returncode is not None
        assert children[0].process.stdout.closed and children[0].process.stderr.closed
    finally:
        if lease is not None:
            lease.close()


@pytest.mark.parametrize("boundary", ("ready", "freeze"))
def test_latched_signal_before_commit_is_accounted(coordinated_helper, monkeypatch, boundary):
    session, writer, events, children = coordinated_helper
    if boundary == "ready":
        poll = remote_helper.SupervisedChild.poll
        injected = False

        def poll_then_signal(child):
            nonlocal injected
            result = poll(child)
            if not injected:
                injected = True
                session.handle_signal(signal.SIGTERM)
            return result

        monkeypatch.setattr(remote_helper.SupervisedChild, "poll", poll_then_signal)
        write_start(
            writer, RemoteProcess((sys.executable, "-c", "import signal;signal.pause()")), ()
        )
    else:
        cleanup_report = session._cleanup_report

        def report_then_signal():
            report = cleanup_report()
            session.handle_signal(signal.SIGTERM)
            return report

        monkeypatch.setattr(session, "_cleanup_report", report_then_signal)
        writer.close()
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert snapshot.outcome.primary_failure is not None
    assert snapshot.outcome.primary_failure.code == "REMOTE_SIGNAL"
    assert not any(kind == "READY" for kind, _ in events)
    assert snapshot.cleanup.confirmed
    assert all(child.process.returncode is not None for child in children)


@pytest.mark.parametrize("boundary", ("ready", "freeze"))
def test_native_signal_delivery_linearizes_after_blocked_commit(
    coordinated_helper, monkeypatch, boundary
):
    session, writer, events, _children = coordinated_helper
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())
    if boundary == "ready":
        emit = remote_helper.emit

        def signal_then_emit(kind, **fields):
            if kind == "READY":
                signal.raise_signal(signal.SIGTERM)
                assert session._signal_scope.pending_signum is None
            emit(kind, **fields)

        monkeypatch.setattr(remote_helper, "emit", signal_then_emit)
        write_start(
            writer, RemoteProcess((sys.executable, "-c", "import signal;signal.pause()")), ()
        )
    else:
        freeze = session.lifecycle.freeze

        def signal_then_freeze(cleanup):
            signal.raise_signal(signal.SIGTERM)
            assert session._signal_scope.pending_signum is None
            return freeze(cleanup)

        monkeypatch.setattr(session.lifecycle, "freeze", signal_then_freeze)
        writer.close()
    _run_terminated(session, fails=True)
    snapshot = _terminal_event(events)
    assert signal.pthread_sigmask(signal.SIG_BLOCK, ()) == previous_mask
    assert session._signal_scope.pending_signum == signal.SIGTERM
    if boundary == "ready":
        assert any(kind == "READY" for kind, _ in events)
        assert snapshot.outcome.primary_failure.code == "REMOTE_SIGNAL"
    else:
        assert snapshot.outcome.primary_failure is None
        assert session.lifecycle.state.local_diagnostics[0].code == "REMOTE_SIGNAL"
    assert snapshot.cleanup.confirmed


def test_native_signal_after_frozen_result_fails_without_second_terminal(
    tmp_path, monkeypatch, control_pipe
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    _, writer = control_pipe
    writer.close()
    run = remote_helper.ControlSession.run
    emit = remote_helper.emit
    events = []

    def run_then_signal(session):
        run(session)
        # The allocation wrapper still owns the native handlers at this point.
        signal.raise_signal(signal.SIGTERM)

    def record(kind, **fields):
        events.append((kind, fields))
        emit(kind, **fields)

    monkeypatch.setattr(remote_helper.ControlSession, "run", run_then_signal)
    monkeypatch.setattr(remote_helper, "emit", record)
    with pytest.raises(SystemExit) as raised:
        remote_helper.control()
    assert raised.value.code == 1
    snapshot = _terminal_event(events)
    assert snapshot.outcome.trigger == Trigger.CONTROLLER_EOF
    assert snapshot.outcome.primary_failure is None
    assert snapshot.cleanup.confirmed
    assert not any(tmp_path.iterdir())


def test_terminal_partial_write_failure_does_not_replay_frozen_snapshot(
    tmp_path, monkeypatch, control_pipe
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session = remote_helper.ControlSession.create()
    _, writer = control_pipe
    writer.close()
    read_fd, write_fd = os.pipe()
    write = os.write
    emit = remote_helper.emit
    terminals = []
    partial = False

    def fail_after_prefix(descriptor, payload):
        nonlocal partial
        if descriptor == write_fd:
            if partial:
                raise BrokenPipeError("terminal prefix already delivered")
            if bytes(payload).startswith(b'{"child_result"'):
                partial = True
                return write(descriptor, payload[:5])
        return write(descriptor, payload)

    def record(kind, **fields):
        if kind == "SESSION_ENDED":
            terminals.append(fields)
        emit(kind, **fields)

    with os.fdopen(read_fd, "rb"), os.fdopen(write_fd, "w", encoding="utf-8") as stdout:
        monkeypatch.setattr(remote_helper.sys, "stdout", stdout)
        monkeypatch.setattr(remote_helper.os, "write", fail_after_prefix)
        monkeypatch.setattr(remote_helper, "emit", record)
        _run_terminated(session, fails=True)
    assert partial
    assert len(terminals) == 1
    assert session.lifecycle.state.snapshot.cleanup.confirmed
    assert session.lifecycle.state.local_diagnostics
    assert session.workspace_lock.closed and not session.work.exists()


def test_workspace_cleanup_timeout_closes_admission_without_removing_live_stage(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session_id, workspace, owner_lock = remote_helper.new_workspace()
    session = remote_helper.ControlSession(session_id, workspace, owner_lock)
    clock = 0.0
    original_flock = remote_helper.fcntl.flock

    def observe_lease_contention(stream, operation):
        nonlocal clock
        try:
            return original_flock(stream, operation)
        except BlockingIOError:
            # Advance at the observed contention, not by scheduling or sleeping.
            clock = remote_helper.WORKSPACE_LEASE_TIMEOUT + 1.0
            raise

    monkeypatch.setattr(remote_helper.time, "monotonic", lambda: clock)
    monkeypatch.setattr(remote_helper.fcntl, "flock", observe_lease_contention)
    with remote_helper._stage_lease(workspace):
        session._release_workspace()
        assert workspace.is_dir()
        assert len(session.cleanup_errors) == 1
        assert isinstance(session.cleanup_errors[0], TimeoutError)
        with pytest.raises(ValueError), remote_helper._stage_lease(workspace):
            pytest.fail("cleanup admitted a new stage")
    # Closure survives the existing lease's release and a failed removal.
    with pytest.raises(ValueError), remote_helper._stage_lease(workspace):
        pytest.fail("failed cleanup reopened staging")
    remote_helper.remove_workspace(workspace)
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.parametrize("failed_suffixes", (("lease",), ("closed",), ("lease", "closed")))
def test_metadata_removal_failure_fails_session_and_is_reclaimed_later(
    tmp_path, monkeypatch, control_pipe, capsys, failed_suffixes
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session_id, workspace, owner_lock = remote_helper.new_workspace()
    session = remote_helper.ControlSession(session_id, workspace, owner_lock)
    metadata = {tmp_path / f".{session_id}.{suffix}" for suffix in failed_suffixes}
    path_type = type(tmp_path)
    original_unlink = path_type.unlink

    def fail_metadata_removal(path, *args, **kwargs):
        if path in metadata:
            raise PermissionError(f"cannot remove {path.name}")
        return original_unlink(path, *args, **kwargs)

    _reader, writer = control_pipe
    writer.close()
    with monkeypatch.context() as patch:
        patch.setattr(path_type, "unlink", fail_metadata_removal)
        with pytest.raises(SystemExit):
            session.run()

    assert not workspace.exists()
    assert owner_lock.closed
    assert set(tmp_path.iterdir()) == metadata
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "SESSION_ENDED"
    snapshot = decode_terminal(events[-1])
    assert snapshot.cleanup.residual_resources == ("workspace_metadata",)
    assert snapshot.outcome.primary_failure is not None
    assert all(suffix in repr(snapshot.outcome) for suffix in failed_suffixes)

    for path in metadata:
        os.utime(path, (1.0, 1.0))
    remote_helper.reclaim_stale_workspaces(tmp_path, now=remote_helper.STALE_SESSION_AGE + 2)
    assert tuple(tmp_path.iterdir()) == ()


def test_cleanup_closes_admission_without_waiting_for_global_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    first_id, first, first_owner = remote_helper.new_workspace()
    second_id, second, second_owner = remote_helper.new_workspace()
    first_session = remote_helper.ControlSession(first_id, first, first_owner)
    second_session = remote_helper.ControlSession(second_id, second, second_owner)
    clock = 0.0
    original_flock = remote_helper.fcntl.flock

    def bounded_flock(stream, operation):
        nonlocal clock
        # Fail a blocking acquisition at the OS boundary instead of deadlocking
        # the regression when the old global gate is held by a stopped owner.
        if operation == fcntl.LOCK_EX:
            raise AssertionError("cleanup attempted an unbounded lock acquisition")
        try:
            return original_flock(stream, operation)
        except BlockingIOError:
            clock += remote_helper.WORKSPACE_LEASE_TIMEOUT + 1.0
            raise

    with remote_helper._stage_lease(first):
        with (tmp_path / ".workspace.lock").open("a+b") as old_gate:
            original_flock(old_gate, fcntl.LOCK_EX)
            monkeypatch.setattr(remote_helper.fcntl, "flock", bounded_flock)
            monkeypatch.setattr(remote_helper.time, "monotonic", lambda: clock)
            first_session._release_workspace()
            assert len(first_session.cleanup_errors) == 1
            assert isinstance(first_session.cleanup_errors[0], TimeoutError)
            with pytest.raises(ValueError), remote_helper._stage_lease(first):
                pytest.fail("timed-out cleanup left admission open")
            second_session._release_workspace()
            assert not second_session.cleanup_errors
            assert not second.exists()
        assert first.is_dir()
    remote_helper.remove_workspace(first)


def test_address_lease_retirement_failure_forbids_collision_retry(coordinated_helper, monkeypatch):
    session, writer, events, children = coordinated_helper
    record = remote_helper.emit
    lease = None

    class FailedLease:
        def close(self):
            raise OSError("old address lease remains owned")

    def emit(kind, **fields):
        nonlocal lease
        record(kind, **fields)
        if kind == "ATTEMPT":
            lease = session._address_lease
            monkeypatch.setattr(session, "_address_lease", FailedLease())

    monkeypatch.setattr(remote_helper, "emit", emit)
    write_start(
        writer,
        RemoteProcess(
            (
                sys.executable,
                "-c",
                "import sys;print('Address already in use',file=sys.stderr,flush=True);sys.exit(1)",
            ),
            required_output_sentinels=("not-ready",),
        ),
        (),
    )
    try:
        _run_terminated(session, fails=True)
        snapshot = _terminal_event(events)
        assert len(children) == 1
        assert [fields["generation"] for kind, fields in events if kind == "ATTEMPT"] == [1]
        assert snapshot.outcome.child_result.generation == 1
        assert any(detail.code == "STARTUP_EXIT" for detail in snapshot.outcome.diagnostics)
        assert "address_lease" in snapshot.cleanup.residual_resources
        assert session.work.is_dir() and session.workspace_lock.closed
    finally:
        if lease is not None:
            lease.close()
