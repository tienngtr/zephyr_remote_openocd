# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import math
import os
import select
import signal
import sys
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "zro_remote_helper", ROOT / "python/zephyr_remote_openocd/remote_helper.py"
)
assert SPEC is not None and SPEC.loader is not None
remote_helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remote_helper)

SAMPLE_CHILD_EXIT_CODE = 7


@pytest.fixture
def control_pipe(monkeypatch):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as reader, os.fdopen(write_fd, "wb", buffering=0) as writer:
        monkeypatch.setattr(remote_helper.sys, "stdin", SimpleNamespace(buffer=reader))
        yield reader, writer


@pytest.mark.parametrize(
    ("interruption", "expected_error", "batch"),
    (
        pytest.param(None, None, False, id="eof"),
        pytest.param(b'{"version":1,"type":"STOP"}\n', None, False, id="stop"),
        pytest.param(b'{"version":1,"type":"STOP"}\n', None, True, id="batched-stop"),
        pytest.param(b"not-json\n", json.JSONDecodeError, False, id="malformed-json"),
        pytest.param(b'{"version":1,"type":"STOP"}', ValueError, False, id="incomplete-eof"),
        pytest.param(b'{"version":1,"type":"UNKNOWN"}\n', ValueError, False, id="unexpected"),
        pytest.param(b"START", ValueError, False, id="duplicate-start"),
    ),
)
def test_control_session_services_input_during_readiness(
    tmp_path, monkeypatch, control_pipe, interruption, expected_error, batch
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    start = (
        json.dumps(
            {
                "version": 1,
                "type": "START",
                "argv": [sys.executable, "-c", "import signal; signal.pause()"],
                "environment": {},
                "required_paths": [],
                "services": [],
                "required_output_sentinels": ["not-ready"],
                "readiness_timeout": 30,
                "literal_prefix": 1,
            }
        ).encode()
        + b"\n"
    )
    original_spawn = remote_helper._spawn_child
    children = []
    events = []

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        if not batch:
            if interruption is None:
                writer.close()
            else:
                writer.write(start if interruption == b"START" else interruption)
                if not interruption.endswith(b"\n") and interruption != b"START":
                    writer.close()
        return child

    def record_event(kind, **values):
        if kind in ("SESSION_CLOSED", "ERROR"):
            assert children[0].process.returncode is not None
            assert not workspace.exists()
            assert lock.closed
        events.append((kind, values))

    writer.write(start + interruption if batch else start)
    monkeypatch.setattr(remote_helper, "emit", record_event)
    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    session = remote_helper.ControlSession("session", workspace, lock)

    session.run()

    assert len(children) == 1
    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed
    assert children[0].process.stderr.closed
    assert not workspace.exists()
    assert lock.closed
    assert not any(kind == "PROCESS_READY" for kind, _values in events)
    if expected_error is not None:
        assert isinstance(session.protocol_error, expected_error)
        assert events[-1][0] == "ERROR"
        assert events[-1][1]["code"] == "PROTOCOL_ERROR"
    else:
        assert session.protocol_error is None
        if interruption is None:
            assert [kind for kind, _values in events] == ["SESSION_CREATED"]
        else:
            assert events[-1] == ("SESSION_CLOSED", {"reason": "requested", "returncode": None})


def test_helper_accepts_json_whitespace_inside_command_frame(tmp_path, control_pipe, capsys):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (workspace / remote_helper.SESSION_LOCK).open("w+b") as lock:
        session = remote_helper.ControlSession("session", workspace, lock)
        writer.write(b' \t{"version":1,"type":"STOP"} \t\r\n')

        session.run()

        assert session.protocol_error is None
        assert not workspace.exists()
        assert lock.closed
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "SESSION_CLOSED"
    assert events[-1]["reason"] == "requested"


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
        "version": 1,
        "type": "START",
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
    }


class _ChunkStream:
    def __init__(self, *chunks):
        self.chunks = list(chunks)


def _decode_chunks(stream, name, sentinels=None, _captured=None):
    if sentinels is None:
        sentinels = remote_helper._RequiredOutputSentinels(())
    decoder = remote_helper._OutputDecoder(name, sentinels)
    for chunk in (*stream.chunks, b""):
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


def test_relay_emits_bounded_fragments_and_preserves_utf8(monkeypatch):
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 4)
    events = []
    monkeypatch.setattr(
        remote_helper,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )
    required_output_sentinels = remote_helper._RequiredOutputSentinels(("READY",))
    captured: list[Any] = []
    stream = _ChunkStream(
        b"abc\n",
        b"defg",
        b"\nxy",
        b"z\nvalid \xe2",
        b"\x82\xac\ninvalid \xff",
        b"tail",
    )

    _decode_chunks(stream, "stdout", required_output_sentinels, captured)

    output_events = [values for _kind, values in events]
    assert [event["payload"] for event in output_events] == [
        "abc",
        "defg",
        "",
        "xy",
        "z",
        "vali",
        "d ",
        "€",
        "inva",
        "lid ",
        "�",
        "tail",
    ]
    assert [event["line_end"] for event in output_events] == [
        True,
        False,
        True,
        False,
        True,
        False,
        False,
        True,
        False,
        False,
        False,
        False,
    ]
    assert (
        "".join(event["payload"] + ("\n" if event["line_end"] else "") for event in output_events)
        == "abc\ndefg\nxyz\nvalid €\ninvalid �tail"
    )
    assert all(len(event["payload"]) <= 4 for event in output_events)
    assert not required_output_sentinels.ready


def test_relay_matches_sentinel_only_after_complete_line(monkeypatch):
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 8)
    events = []
    monkeypatch.setattr(
        remote_helper,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )
    required_output_sentinels = remote_helper._RequiredOutputSentinels(("READY FOR START",))
    stream = _ChunkStream(b"NO\nREADY FOR ", b"START\n")

    _decode_chunks(stream, "stderr", required_output_sentinels, [])

    assert required_output_sentinels.ready
    output = [values for _kind, values in events]
    assert "".join(item["payload"] + ("\n" if item["line_end"] else "") for item in output) == (
        "NO\nREADY FOR START\n"
    )


@pytest.mark.parametrize(
    ("first", "second"),
    (
        ("OPENOCD_INIT", "STARTUP_COMPLETE"),
        ("STARTUP_COMPLETE", "OPENOCD_INIT"),
    ),
    ids=("init-first", "startup-first"),
)
def test_relay_waits_for_each_complete_sentinel_across_streams(monkeypatch, first, second):
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 8)
    monkeypatch.setattr(remote_helper, "emit", lambda *_args, **_kwargs: None)
    required_output_sentinels = remote_helper._RequiredOutputSentinels(
        ("OPENOCD_INIT", "STARTUP_COMPLETE")
    )

    _decode_chunks(
        _ChunkStream(first[:8].encode(), (first[8:] + "\n").encode()),
        "stdout",
        required_output_sentinels,
    )

    assert not required_output_sentinels.ready

    _decode_chunks(
        _ChunkStream(("  " + second + "  \n").encode()),
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

    _decode_chunks(_ChunkStream(payload), "stdout")

    output_events = [values for _kind, values in events]
    reconstructed = "".join(
        event["payload"] + ("\n" if event["line_end"] else "") for event in output_events
    )
    assert reconstructed == expected
    if payload == b"abcdefghij\n":
        assert [event["payload"] for event in output_events] == ["abcd", "efgh", "ij"]
        assert [event["line_end"] for event in output_events] == [False, False, True]
    elif payload == b"tail":
        assert output_events == [
            {"stream": "stdout", "payload": "tail", "line_end": False},
        ]
    elif payload == b"\n\nx\n":
        assert [(event["payload"], event["line_end"]) for event in output_events] == [
            ("", True),
            ("", True),
            ("x", True),
        ]
    else:
        assert output_events == []


def test_relay_preserves_split_utf8_and_invalid_bytes(monkeypatch):
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 3)
    events = []
    monkeypatch.setattr(
        remote_helper,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )

    _decode_chunks(_ChunkStream(b"utf \xe2", b"\x82", b"\xac\ninvalid \xff"), "stderr")

    output_events = [values for _kind, values in events]
    assert (
        "".join(event["payload"] + ("\n" if event["line_end"] else "") for event in output_events)
        == "utf €\ninvalid �"
    )
    assert all(len(event["payload"]) <= 3 for event in output_events)


def test_relay_real_child_flushes_newline_free_output_before_exit(
    tmp_path, monkeypatch, control_pipe
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 64)
    session = remote_helper.ControlSession("session", workspace, lock)
    output_size = remote_helper.MAX_CAPTURED_STARTUP_FRAGMENTS * 64 + 1
    events = []

    def emit(kind, **values):
        events.append((kind, values))
        if kind == "CHILD_OUTPUT":
            assert session.child is not None
            assert len(session.child.startup_output) <= remote_helper.MAX_CAPTURED_STARTUP_FRAGMENTS
            output = [values["payload"] for kind, values in events if kind == "CHILD_OUTPUT"]
            if len("".join(output)) == output_size:
                assert session.child.poll() is None
                writer.write(b'{"version":1,"type":"STOP"}\n')

    monkeypatch.setattr(remote_helper, "emit", emit)
    writer.write(
        json.dumps(
            {
                "version": 1,
                "type": "START",
                "argv": [
                    sys.executable,
                    "-c",
                    "import signal,sys;"
                    f"sys.stdout.buffer.write(b'x'*{output_size});sys.stdout.flush();signal.pause()",
                ],
                "environment": {},
                "required_paths": [],
                "services": [],
                "required_output_sentinels": [],
                "readiness_timeout": 30,
                "literal_prefix": 3,
            }
        ).encode()
        + b"\n"
    )

    session.run()

    output = [values for kind, values in events if kind == "CHILD_OUTPUT"]
    assert output
    assert all(len(values["payload"]) <= 64 for values in output)
    assert events[-1][0] == "SESSION_CLOSED"


def test_spawn_child_rolls_back_process_when_ownership_wrapper_fails(monkeypatch):
    original_popen = remote_helper.subprocess.Popen
    processes = []
    process_pidfds = []
    failure = RuntimeError("injected child ownership failure")

    def capture_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        process_pidfds.append(os.pidfd_open(process.pid))
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


def test_new_workspace_holds_exclusive_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    _session_id, workspace, owner_lock = remote_helper.new_workspace()
    observer = (workspace / remote_helper.SESSION_LOCK).open("r+b")
    try:
        try:
            fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError("session workspace was not exclusively locked")
    finally:
        observer.close()
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


@pytest.mark.parametrize("output_cleanup_fails", [False, True])
def test_control_session_cleans_up_when_announcement_fails(
    tmp_path, monkeypatch, output_cleanup_fails
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session_id, workspace, lock = remote_helper.new_workspace()

    failure = BrokenPipeError("injected announcement failure")

    def fail_announce(_session):
        raise failure

    monkeypatch.setattr(remote_helper.ControlSession, "announce", fail_announce)
    if output_cleanup_fails:

        def fail_output_cleanup(_output):
            raise OSError("injected output fd restoration failure")

        monkeypatch.setattr(remote_helper._ProtocolOutput, "close", fail_output_cleanup)
    session = remote_helper.ControlSession(session_id, workspace, lock)

    with pytest.raises(BrokenPipeError) as raised:
        session.run()

    assert raised.value is failure
    if output_cleanup_fails:
        assert any("fd restoration failure" in note for note in failure.__notes__)
    assert not workspace.exists()
    assert lock.closed


def test_control_session_cleans_up_when_protocol_output_setup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session_id, workspace, lock = remote_helper.new_workspace()
    session = remote_helper.ControlSession(session_id, workspace, lock)
    failure = OSError("injected output fd setup failure")

    def fail_output_setup(_descriptor):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(remote_helper.sys, "stdout", sys.__stdout__)
        patch.setattr(remote_helper.os, "get_blocking", fail_output_setup)
        with pytest.raises(OSError) as raised:
            session.run()

    assert raised.value is failure
    assert not workspace.exists()
    assert lock.closed


def _start_session_command(argv, sentinels=()):
    return (
        json.dumps(
            {
                "version": 1,
                "type": "START",
                "argv": argv,
                "environment": {},
                "required_paths": [],
                "services": [],
                "required_output_sentinels": list(sentinels),
                "readiness_timeout": 30,
                "literal_prefix": len(argv),
            }
        ).encode()
        + b"\n"
    )


def test_control_session_cleanup_attempts_all_resources_once(tmp_path, monkeypatch, control_pipe):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    failure = RuntimeError("child cleanup failed")
    original_spawn = remote_helper._spawn_child
    children = []
    calls = []

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        terminate = child.terminate

        async def terminate_then_fail():
            calls.append("terminate")
            session.handle_signal()
            await terminate()
            raise failure

        monkeypatch.setattr(child, "terminate", terminate_then_fail)
        return child

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    writer.write(
        _start_session_command(
            [sys.executable, "-c", "import signal;signal.pause()"], ("not-ready",)
        )
        + b'{"version":1,"type":"STOP"}\n'
    )
    with pytest.raises(RuntimeError) as raised:
        session.run()

    assert raised.value is failure
    assert calls == ["terminate"]
    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert lock.closed
    assert not workspace.exists()


@pytest.mark.parametrize("signal_source", ("callback", "os"))
def test_control_session_signal_during_spawn_terminates_owned_child(
    tmp_path, monkeypatch, control_pipe, signal_source
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    original_spawn = remote_helper._spawn_child
    spawned = []
    events = []
    in_signal_handler = False
    queue_mutations_in_handler = []
    original_handler = session.handle_signal
    original_put = session._signals.put_nowait

    def handle_signal(signum, frame=None):
        nonlocal in_signal_handler
        in_signal_handler = True
        try:
            original_handler(signum, frame)
        finally:
            in_signal_handler = False

    def record_signal(signum):
        queue_mutations_in_handler.append(in_signal_handler)
        original_put(signum)

    monkeypatch.setattr(session, "handle_signal", handle_signal)
    monkeypatch.setattr(session._signals, "put_nowait", record_signal)

    def spawn_then_signal(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        spawned.append(child)
        if signal_source == "os":
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            session.handle_signal(signal.SIGTERM)
        return child

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn_then_signal)
    monkeypatch.setattr(remote_helper, "emit", lambda kind, **values: events.append((kind, values)))
    writer.write(_start_session_command([sys.executable, "-c", "import signal;signal.pause()"]))

    session.run()

    assert len(spawned) == 1
    assert spawned[0].process.returncode is not None
    assert queue_mutations_in_handler and not any(queue_mutations_in_handler)
    assert not any(kind == "PROCESS_READY" for kind, _ in events)
    assert not workspace.exists()
    assert lock.closed


def test_control_session_signal_during_spawn_preserves_spawn_failure(
    tmp_path, monkeypatch, control_pipe
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    failure = OSError("injected spawn failure")

    def signal_then_fail(*_args, **_kwargs):
        session.handle_signal()
        raise failure

    monkeypatch.setattr(remote_helper, "_spawn_child", signal_then_fail)
    writer.write(_start_session_command(["openocd"]))

    session.run()

    assert session.protocol_error is failure
    assert not workspace.exists()
    assert lock.closed


def test_output_observer_failure_ends_session_and_cancels_tasks(
    tmp_path, monkeypatch, control_pipe
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    children = []
    original_spawn = remote_helper._spawn_child
    original_read = remote_helper._AsyncInput.read
    failure = OSError("injected pipe read failure")

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    async def fail_output_read(reader):
        if children and reader.descriptor == children[0].process.stdout.fileno():
            raise failure
        return await original_read(reader)

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper._AsyncInput, "read", fail_output_read)
    writer.write(
        _start_session_command(
            [sys.executable, "-c", "import signal;signal.pause()"], ("not-ready",)
        )
    )

    async def run():
        tasks_before = asyncio.all_tasks()
        with pytest.raises(OSError) as raised:
            await session.run_async()
        assert raised.value is failure
        assert asyncio.all_tasks() == tasks_before

    asyncio.run(run())

    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert lock.closed
    assert not workspace.exists()


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
        if kind == "PROCESS_READY":
            writer.write(b'{"version":1,"type":"STOP"}\n')

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

    session.run()

    if final_observation == "ready":
        assert session.protocol_error is None
        assert [kind for kind, _ in events].count("PROCESS_READY") == 1
        assert events[-1][0] == "SESSION_CLOSED"
    else:
        assert isinstance(session.protocol_error, RuntimeError)
        assert not any(kind == "PROCESS_READY" for kind, _ in events)
        assert events[-1][0] == "ERROR"
        if final_observation == "exit":
            assert str(SAMPLE_CHILD_EXIT_CODE) in str(session.protocol_error)
    assert children[0].process.returncode is not None
    assert not workspace.exists()
    assert lock.closed


def test_cancelled_session_cleans_child_before_leaving_task_scope(
    tmp_path, monkeypatch, control_pipe
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    children = []
    original_spawn = remote_helper._spawn_child

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    def emit(kind, **_values):
        if kind == "PROCESS_READY":
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper, "emit", emit)
    writer.write(_start_session_command([sys.executable, "-c", "import signal;signal.pause()"]))

    async def run():
        tasks_before = asyncio.all_tasks()
        with pytest.raises(asyncio.CancelledError):
            await session.run_async()
        assert asyncio.all_tasks() == tasks_before

    asyncio.run(run())

    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert not workspace.exists()
    assert lock.closed


def test_output_drain_deadline_cancels_readers_and_closes_streams(
    tmp_path, monkeypatch, control_pipe
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    children = []
    original_spawn = remote_helper._spawn_child
    original_read = remote_helper._AsyncInput.read
    original_sleep = remote_helper.asyncio.sleep
    blocked = asyncio.Event()

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    async def held_reader(reader):
        if children and reader.descriptor in (
            children[0].process.stdout.fileno(),
            children[0].process.stderr.fileno(),
        ):
            await blocked.wait()
        return await original_read(reader)

    async def controlled_sleep(delay):
        if delay != remote_helper.CHILD_RELAY_JOIN_TIMEOUT:
            await original_sleep(delay)

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper._AsyncInput, "read", held_reader)
    monkeypatch.setattr(remote_helper.asyncio, "sleep", controlled_sleep)
    writer.write(
        _start_session_command(
            [sys.executable, "-c", "import signal;signal.pause()"], ("not-ready",)
        )
        + b'{"version":1,"type":"STOP"}\n'
    )

    async def run():
        tasks_before = asyncio.all_tasks()
        with pytest.raises(RuntimeError):
            await session.run_async()
        assert asyncio.all_tasks() == tasks_before

    asyncio.run(run())

    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert not workspace.exists()
    assert lock.closed


def test_output_delivery_failure_during_shutdown_remains_fatal(tmp_path, monkeypatch, control_pipe):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    events = []
    failure = BrokenPipeError("injected shutdown output failure")

    def emit(kind, **values):
        if kind == "CHILD_OUTPUT" and "shutdown" in values["payload"]:
            raise failure
        events.append((kind, values))
        if kind == "PROCESS_READY":
            writer.write(b'{"version":1,"type":"STOP"}\n')

    monkeypatch.setattr(remote_helper, "emit", emit)
    writer.write(
        _start_session_command(
            [
                sys.executable,
                "-c",
                "import signal,sys;"
                "signal.signal(signal.SIGTERM,"
                "lambda *_:(print('shutdown',flush=True),sys.exit(0)));"
                "print('ready',flush=True);signal.pause()",
            ],
            ("ready",),
        )
    )

    with pytest.raises(BrokenPipeError) as raised:
        session.run()

    assert raised.value is failure
    assert any(kind == "ERROR" for kind, _ in events)
    assert not any(kind == "SESSION_CLOSED" for kind, _ in events)
    assert lock.closed
    assert not workspace.exists()


def test_control_session_natural_exit_cleanup_failure_emits_error_event(
    tmp_path, monkeypatch, control_pipe
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    events = []
    monkeypatch.setattr(remote_helper, "emit", lambda kind, **values: events.append((kind, values)))
    original_rmtree = remote_helper.shutil.rmtree
    failure = OSError("injected workspace removal failure")

    def fail_workspace_removal(path):
        if path == workspace:
            raise failure
        original_rmtree(path)

    monkeypatch.setattr(remote_helper.shutil, "rmtree", fail_workspace_removal)
    session = remote_helper.ControlSession("session", workspace, lock)
    writer.write(_start_session_command([sys.executable, "-c", "pass"]))

    with pytest.raises(OSError) as raised:
        session.run()
    assert raised.value is failure

    assert events[-1][0] == "ERROR"
    assert sum(kind == "ERROR" for kind, _ in events) == 1
    assert not any(kind == "SESSION_CLOSED" for kind, _ in events)
    assert workspace.exists()
    assert lock.closed
    original_rmtree(workspace)


def test_protocol_error_remains_primary_when_cleanup_also_fails(
    tmp_path, monkeypatch, control_pipe
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    events = []
    monkeypatch.setattr(
        remote_helper,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )
    original_rmtree = remote_helper.shutil.rmtree

    def fail_workspace_removal(path):
        if path == workspace:
            raise OSError("injected workspace removal failure")
        original_rmtree(path)

    session = remote_helper.ControlSession("session", workspace, lock)
    monkeypatch.setattr(remote_helper.ControlSession, "create", lambda: session)
    monkeypatch.setattr(remote_helper.shutil, "rmtree", fail_workspace_removal)
    monkeypatch.setattr(remote_helper.sys, "argv", ["remote_helper.py", "control"])

    _reader, writer = control_pipe
    writer.write(b"not-json\n")

    with pytest.raises(SystemExit) as raised:
        remote_helper.main()

    assert raised.value.code == 1
    assert [kind for kind, _values in events] == ["SESSION_CREATED", "ERROR"]
    assert events[-1][1]["code"] == "PROTOCOL_ERROR"
    assert any(
        note.startswith("session cleanup also failed:") for note in session.protocol_error.__notes__
    )
    assert any(
        "injected workspace removal failure" in note for note in session.protocol_error.__notes__
    )
    assert isinstance(events[-1][1]["message"], str)
    assert events[-1][1]["message"]
    assert workspace.exists()
    assert lock.closed

    monkeypatch.undo()
    original_rmtree(workspace)


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
        _assert_pidfd_exited(descendant_pidfd)
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
        _assert_pidfd_exited(descendant_pidfd)
        assert str(descendant_pid) in capfd.readouterr().err
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
    signals = []
    monkeypatch.setattr(remote_helper.os, "killpg", lambda _pid, signum: signals.append(signum))

    async def leader_exited():
        return True

    monkeypatch.setattr(child, "_wait_for_leader_exit", leader_exited)
    monkeypatch.setattr(child, "_group_exists", lambda: True)
    monkeypatch.setattr(
        child,
        "_warn_remaining_group_members",
        lambda: (_ for _ in ()).throw(OSError("proc unavailable")),
    )

    asyncio.run(child.terminate())

    assert signals.count(signal.SIGTERM) == 1
    assert signals.count(signal.SIGKILL) == 1
    assert signals.index(signal.SIGTERM) < signals.index(signal.SIGKILL)


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
    signal_error = RuntimeError("signal failed")
    signals = []

    def fail_signal(_pid, signum):
        signals.append(signum)
        if signum != 0:
            raise signal_error

    async def fail_leader_wait():
        raise RuntimeError("leader wait failed")

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
    with pytest.raises(ValueError, match="invalid required-path assertion"):
        remote_helper.decode_command(start_command)


def test_control_session_does_not_launch_when_required_file_is_missing(
    tmp_path, monkeypatch, start_command, control_pipe
):
    workspace = tmp_path / "workspace"
    staged = workspace / "staged"
    staged.mkdir(parents=True)
    missing_file = staged / "image"
    start_command["required_paths"] = [{"kind": "file", "path": "{workspace}/staged/image"}]
    spawn_calls = []
    monkeypatch.setattr(
        remote_helper,
        "_spawn_child",
        lambda *args, **kwargs: spawn_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(remote_helper, "allocate_service_address", lambda _ports: "127.0.0.1")
    _reader, writer = control_pipe
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    writer.write(json.dumps(start_command).encode() + b"\n")

    session.run()

    assert isinstance(session.protocol_error, ValueError)
    message = str(session.protocol_error)
    assert "required remote file" in message
    assert "missing" in message
    assert str(missing_file) in message
    assert spawn_calls == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("argv", [], "argv"),
        ("environment", {"BAD=NAME": "value"}, "environment"),
        ("required_output_sentinels", [" READY "], "markers"),
        ("required_output_sentinels", ["READY", "READY"], "unique"),
        ("readiness_timeout", 0, "readiness options"),
        ("literal_prefix", 3, "readiness options"),
    ),
)
def test_decode_command_rejects_invalid_start_values(start_command, field, value, message):
    start_command[field] = value
    with pytest.raises(ValueError, match=message):
        remote_helper.decode_command(start_command)


@pytest.mark.parametrize(
    ("services", "message"),
    (
        (
            [
                {"name": "gdb", "remote_port": 3333},
                {"name": "gdb", "remote_port": 6333},
            ],
            "unique names",
        ),
        (
            [
                {"name": "gdb", "remote_port": 3333},
                {"name": "tcl", "remote_port": 3333},
            ],
            "unique remote ports",
        ),
    ),
)
def test_decode_command_rejects_duplicate_services(start_command, services, message):
    start_command["services"] = services
    with pytest.raises(ValueError, match=message):
        remote_helper.decode_command(start_command)


def test_decode_command_rejects_unknown_start_and_stop_fields(start_command):
    start_command["future"] = True
    with pytest.raises(ValueError, match="START fields"):
        remote_helper.decode_command(start_command)
    with pytest.raises(ValueError, match="STOP fields"):
        remote_helper.decode_command({"version": 1, "type": "STOP", "future": True})
