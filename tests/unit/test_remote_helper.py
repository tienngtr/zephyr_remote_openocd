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
from typing import IO, Any
from unittest.mock import create_autospec

import pytest
from zephyr_remote_openocd.remote.protocol import decode_message

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


@pytest.mark.parametrize(
    ("interruption", "expected_error", "batch", "sentinels"),
    (
        pytest.param(
            b'{"version":1,"type":"STOP"}\n', None, True, ["not-ready"], id="batched-stop"
        ),
        pytest.param(b'{"version":1,"type":"STOP"}\n', None, True, [], id="immediate-batched-stop"),
        pytest.param(
            b"not-json\n", json.JSONDecodeError, False, ["not-ready"], id="malformed-json"
        ),
        pytest.param(
            b'{"version":1,"type":"STOP"}', ValueError, False, ["not-ready"], id="incomplete-eof"
        ),
        pytest.param(
            b'{"version":1,"type":"UNKNOWN"}\n', ValueError, False, ["not-ready"], id="unexpected"
        ),
        pytest.param(b"START", ValueError, False, ["not-ready"], id="duplicate-start"),
    ),
)
def test_control_session_services_input_during_readiness(
    tmp_path, monkeypatch, control_pipe, interruption, expected_error, batch, sentinels
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
                "required_output_sentinels": sentinels,
                "readiness_timeout": 30,
                "literal_prefix": 1,
                "argv_templates": [],
                "preferred_address": None,
            }
        ).encode()
        + b"\n"
    )
    original_spawn = remote_helper._spawn_child
    children = []
    events: list[tuple[str, dict[str, Any]]] = []

    def spawn(*args, **kwargs):
        assert events[-1] == ("PROCESS_STARTING", {"argv": list(args[0])})
        child = original_spawn(*args, **kwargs)
        children.append(child)
        if not batch:
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
        assert events[-1] == ("SESSION_CLOSED", {"reason": "requested", "returncode": None})


@pytest.mark.parametrize("termination", ("stop", "eof", "invalid-command"))
def test_readiness_honors_control_fact_blocked_at_publication(
    tmp_path, monkeypatch, control_pipe, capsys, termination
):
    reader, writer = control_pipe
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session = remote_helper.ControlSession.create()
    recognized = asyncio.Event()
    publish = asyncio.Event()
    children = []
    original_put = session._events.put
    original_read = remote_helper._AsyncInput.read
    original_spawn = remote_helper._spawn_child
    original_emit = remote_helper.emit

    async def put(observation):
        if session.request is not None and isinstance(
            observation, (remote_helper._ControlFrame, remote_helper._ControlEOF)
        ):
            recognized.set()
            await publish.wait()
        await original_put(observation)

    async def read(source):
        if source.descriptor != reader.fileno():
            await recognized.wait()
        return await original_read(source)

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        if termination == "eof":
            writer.close()
        else:
            writer.write(
                b'{"version":1,"type":"STOP"}\n' if termination == "stop" else b'not-json\n'
            )
        return child

    def emit(kind, **values):
        original_emit(kind, **values)
        if kind == "PROCESS_READY":
            # Release a buggy implementation so its assertion fails after cleanup.
            publish.set()

    monkeypatch.setattr(session._events, "put", put)
    monkeypatch.setattr(remote_helper._AsyncInput, "read", read)
    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper, "emit", emit)
    writer.write(
        _start_session_command(
            [sys.executable, "-c", "import signal;print('ready',flush=True);signal.pause()"],
            ("ready",),
        )
    )

    session.run()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert recognized.is_set()
    assert not any(event["type"] == "PROCESS_READY" for event in events)
    if termination == "stop":
        assert events[-1]["type"] == "SESSION_CLOSED"
        assert events[-1]["reason"] == "requested"
    elif termination == "invalid-command":
        assert events[-1]["type"] == "ERROR"
        assert events[-1]["code"] == "PROTOCOL_ERROR"
    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert session.workspace_lock.closed and not any(tmp_path.iterdir())


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
                "argv_templates": [],
                "preferred_address": None,
            }
        ).encode()
        + b"\n"
    )

    session.run()

    output = [values for kind, values in events if kind == "CHILD_OUTPUT"]
    assert output
    assert all(len(values["payload"]) <= 64 for values in output)
    assert events[-1][0] == "SESSION_CLOSED"


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
        with pytest.raises(RuntimeError if boundary == "constructor" else SystemExit) as raised:
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


def test_control_session_cleans_up_when_announcement_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session_id, workspace, lock = remote_helper.new_workspace()

    failure = BrokenPipeError("injected announcement failure")

    def fail_announce(_session):
        raise failure

    monkeypatch.setattr(remote_helper.ControlSession, "announce", fail_announce)
    session = remote_helper.ControlSession(session_id, workspace, lock)

    with pytest.raises(BrokenPipeError) as raised:
        session.run()

    assert raised.value is failure
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
        with pytest.raises(failure_type) as raised:
            await session.run_async()
        assert raised.value is failure
        assert asyncio.all_tasks() == tasks_before
        assert remote_helper._protocol_output.get() is output_before

    try:
        asyncio.run(run())
        assert set(restored) == set(previous)
        assert all(signal.getsignal(signum) == handler for signum, handler in previous.items())
        if restoration_fails:
            assert any(str(restoration_failure) in note for note in failure.__notes__)
            assert any(restoration_detail in note for note in failure.__notes__)
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
    writer.write(b'{"version":1,"type":"STOP"}\n')
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
        with pytest.raises(failure_type) as raised:
            session.run()
        assert set(restored) == set(previous)
        assert raised.value is failures[signal.SIGTERM]
        assert any(str(failures[signal.SIGINT]) in note for note in raised.value.__notes__)
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
    control_writer.write(b'{"version":1,"type":"STOP"}\n')
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
        expected = primary if primary is not None else writer_failure
        with pytest.raises(BaseException) as raised:
            await session.run_async()
        assert raised.value is expected
        assert any(str(close_failure) in note for note in expected.__notes__)
        assert any(close_detail in note for note in expected.__notes__)
        if primary is not None:
            assert any(str(writer_failure) in note for note in expected.__notes__)
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


def test_failed_address_lease_retirement_prevents_replacement_spawn(
    tmp_path, monkeypatch, control_pipe
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = remote_helper.ControlSession.create()
    _reader, writer = control_pipe
    original_allocate = remote_helper.allocate_service_address
    original_spawn = remote_helper._spawn_child
    children = []
    failure = OSError("address lease retirement failed")
    leases = []

    def allocate(*args, **kwargs):
        allocated = original_allocate(*args, **kwargs)
        lease = allocated.lease
        leases.append(lease)
        controlled = create_autospec(socket.socket, instance=True, spec_set=True)

        def retire():
            lease.close()
            if controlled.close.call_count == 1:
                raise failure

        controlled.close.side_effect = retire
        allocated.lease = controlled
        return allocated

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(remote_helper, "allocate_service_address", allocate)
    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    writer.write(
        _start_session_command(
            [
                sys.executable,
                "-c",
                "import sys;print('Address already in use',file=sys.stderr,flush=True)",
            ],
            ("not-ready",),
        )
    )
    try:
        try:
            session.run()
        except OSError as raised:
            assert raised is failure
        assert len(children) == 1
        assert session.protocol_error is failure
        assert children[0].process.returncode is not None
        assert not session.work.exists()
        assert session.workspace_lock.closed
    finally:
        for lease in leases:
            lease.close()
        for child in children:
            _cleanup_test_child(child, None)


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
                "argv_templates": [],
                "preferred_address": None,
            }
        ).encode()
        + b"\n"
    )


@pytest.mark.parametrize(
    ("interruption", "expected_error", "publication"),
    (
        pytest.param(b'{"version":1,"type":"STOP"}\n', None, "queued", id="queued-stop"),
        pytest.param(None, None, "queued", id="queued-eof"),
        pytest.param(b"not-json\n", json.JSONDecodeError, "queued", id="queued-malformed"),
        pytest.param(b"START", ValueError, "queued", id="queued-duplicate-start"),
        pytest.param(
            b'{"version":1,"type":"STOP"}\n', None, "queued-signal", id="stop-before-signal"
        ),
        pytest.param(
            b"not-json\n", json.JSONDecodeError, "queued-signal", id="malformed-before-signal"
        ),
        pytest.param(b"START", ValueError, "queued-signal", id="duplicate-start-before-signal"),
        pytest.param(b'{"version":1,"type":"STOP"}\n', None, "pending", id="pending-stop"),
        pytest.param(None, None, "pending", id="pending-eof"),
        pytest.param(b"not-json\n", json.JSONDecodeError, "pending", id="pending-malformed"),
        pytest.param(b"START", ValueError, "pending", id="pending-duplicate-start"),
        pytest.param(b'{"version":1,"type":"STOP"}', ValueError, "pending", id="partial-eof"),
        pytest.param(b'{"version":1,"type":"STOP"}\n', None, "full", id="backpressure-stop"),
        pytest.param(b"SIGNAL", None, "pending", id="latched-signal"),
    ),
)
def test_retry_does_not_spawn_after_terminal_control_observed_during_cleanup(
    tmp_path, monkeypatch, control_pipe, interruption, expected_error, publication
):
    _reader, writer = control_pipe
    workspace = tmp_path / "workspace"
    (workspace / "staged").mkdir(parents=True)
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")
    session = remote_helper.ControlSession("session", workspace, lock)
    consumed = asyncio.Event()
    publish = asyncio.Event()
    pending = []
    children = []
    events = []
    original_put = session._events.put
    original_spawn = remote_helper._spawn_child
    start = _start_session_command(
        [
            sys.executable,
            "-c",
            "import sys;print('Address already in use',file=sys.stderr,flush=True)",
        ],
        ("not-ready",),
    )

    async def controlled_put(observation):
        terminal_control = (
            isinstance(observation, (remote_helper._ControlFrame, remote_helper._ControlEOF))
            and session.request is not None
        ) or (
            isinstance(observation, remote_helper._ObservationFailed)
            and observation.source == "control"
        )
        if terminal_control:
            pending.append(observation)
            consumed.set()
            await publish.wait()
            if publication in ("queued", "queued-signal"):
                return
        await original_put(observation)

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        if len(children) != 1:
            # Keep a buggy extra launch owned and ensure the failing test exits.
            session.handle_signal()
            return child
        terminate = child.terminate
        close_streams = child.close_streams

        async def terminate_then_observe_control():
            await terminate()
            if interruption == b"SIGNAL":
                return
            if interruption is None:
                writer.close()
            else:
                writer.write(start if interruption == b"START" else interruption)
                if interruption != b"START" and not interruption.endswith(b"\n"):
                    writer.close()
            await consumed.wait()

        def close_then_release_publication():
            close_streams()
            if interruption == b"SIGNAL":
                session.handle_signal()
            elif publication in ("queued", "queued-signal"):
                session._events.put_nowait(pending[0])
                if publication == "queued-signal":
                    session.handle_signal()
            elif publication == "full":
                for _ in range(session._events.maxsize):
                    session._events.put_nowait(remote_helper._ChildOutput(child, "stdout", b""))
            publish.set()

        monkeypatch.setattr(child, "terminate", terminate_then_observe_control)
        monkeypatch.setattr(child, "close_streams", close_then_release_publication)
        return child

    monkeypatch.setattr(session._events, "put", controlled_put)
    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper, "emit", lambda kind, **values: events.append((kind, values)))
    writer.write(start)

    async def run():
        tasks_before = asyncio.all_tasks()
        await session.run_async()
        assert asyncio.all_tasks() == tasks_before

    asyncio.run(run())

    assert len(children) == 1
    assert [kind for kind, _values in events].count("PROCESS_STARTING") == 1
    assert not any(kind == "PROCESS_READY" for kind, _values in events)
    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert not workspace.exists()
    assert lock.closed
    if expected_error is not None:
        assert isinstance(session.protocol_error, expected_error)
        assert events[-1][0] == "ERROR"
        assert events[-1][1]["code"] == "PROTOCOL_ERROR"
    else:
        assert session.protocol_error is None
        if interruption == b'{"version":1,"type":"STOP"}\n':
            assert events[-1] == ("SESSION_CLOSED", {"reason": "requested", "returncode": None})


@pytest.mark.parametrize("with_start", (False, True), ids=("without-child", "with-child"))
@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    ("command", "expected_error"),
    (
        pytest.param(b'{"version":1,"type":"STOP"}\n', None, id="stop"),
        pytest.param(b"not-json\n", json.JSONDecodeError, id="malformed-json"),
        pytest.param(b'{"version":1,"type":"UNKNOWN"}\n', ValueError, id="unexpected-command"),
        pytest.param(b"{", ValueError, id="incomplete-eof"),
    ),
)
def test_control_fact_published_before_signal_keeps_its_outcome(
    tmp_path, monkeypatch, control_pipe, capsys, with_start, command, expected_error
):
    _reader, writer = control_pipe
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session = remote_helper.ControlSession.create()
    published = asyncio.Event()
    original_put = session._events.put
    original_coordinate = session._coordinate
    original_spawn = remote_helper._spawn_child
    children = []

    async def publish(observation):
        await original_put(observation)
        if (
            isinstance(observation, remote_helper._ControlFrame) and observation.frame == command
        ) or (
            isinstance(observation, remote_helper._ObservationFailed)
            and observation.source == "control"
        ):
            session.handle_signal(signal.SIGTERM)
            published.set()

    async def coordinate_after_publication():
        # Hold dispatch until the real control observer has queued the fact
        # and the later signal is latched; no scheduler timing is required.
        await published.wait()
        await original_coordinate()

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(session._events, "put", publish)
    monkeypatch.setattr(session, "_coordinate", coordinate_after_publication)
    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    if with_start:
        writer.write(
            _start_session_command(
                [sys.executable, "-c", "import signal;signal.pause()"], ("not-ready",)
            )
        )
    writer.write(command)
    if not command.endswith(b"\n"):
        writer.close()

    session.run()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    if expected_error is None:
        assert session.protocol_error is None
        assert events[-1]["type"] == "SESSION_CLOSED"
        assert events[-1]["reason"] == "requested"
    else:
        assert isinstance(session.protocol_error, expected_error)
        assert events[-1]["type"] == "ERROR"
        assert events[-1]["code"] == "PROTOCOL_ERROR"
    assert not session.cleanup_errors
    assert not any(event["type"] == "PROCESS_READY" for event in events)
    assert len(children) == int(with_start)
    for child in children:
        assert child.process.returncode is not None
        assert child.process.stdout.closed and child.process.stderr.closed
    assert session.workspace_lock.closed and not any(tmp_path.iterdir())


@pytest.mark.parametrize("publication", ("queued", "pending"))
@pytest.mark.parametrize("cleanup_fails", (False, True))
@pytest.mark.timeout(60)
def test_natural_close_preserves_control_failure_consumed_before_dispatch(
    tmp_path, monkeypatch, control_pipe, capsys, publication, cleanup_fails
):
    _reader, writer = control_pipe
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session = remote_helper.ControlSession.create()
    release = tmp_path / "child-release"
    os.mkfifo(release)
    children = []
    failures = []
    grouped = asyncio.Event()
    stdout_finished = asyncio.Event()
    failure_consumed = asyncio.Event()
    final_eof_published = asyncio.Event()
    blocked_publication = asyncio.Event()
    cleanup_error = OSError("process-group cleanup failed")
    original_spawn = remote_helper._spawn_child
    original_read = remote_helper._AsyncInput.read
    original_put = session._events.put
    original_emit = remote_helper.emit

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        terminate = child.terminate

        async def terminate_then_fail():
            await terminate()
            if cleanup_fails:
                raise cleanup_error

        monkeypatch.setattr(child, "terminate", terminate_then_fail)
        return child

    async def read(source):
        chunk = await original_read(source)
        if children and not chunk and source.descriptor == children[0].process.stderr.fileno():
            # Deliver the final child EOF only after the real control reader
            # has recognized its framing failure during group cleanup.
            await grouped.wait()
            await stdout_finished.wait()
            await failure_consumed.wait()
        return chunk

    async def publish(observation):
        if isinstance(observation, remote_helper._GroupCleaned):
            writer.write(b"{")
            writer.close()
            await failure_consumed.wait()
        if (
            isinstance(observation, remote_helper._ObservationFailed)
            and observation.source == "control"
        ):
            failures.append(observation.exception)
            failure_consumed.set()
            await final_eof_published.wait()
            if publication == "pending":
                # Model a put suspended by bounded-queue backpressure. The
                # real guard must retain the failure when shutdown cancels it.
                await blocked_publication.wait()
        await original_put(observation)
        if isinstance(observation, remote_helper._GroupCleaned):
            grouped.set()
        elif isinstance(observation, remote_helper._ChildOutput) and not observation.chunk:
            if observation.stream == "stdout":
                stdout_finished.set()
            else:
                final_eof_published.set()

    def emit(kind, **values):
        original_emit(kind, **values)
        if kind == "PROCESS_READY":
            # FIFO open/EOF handshakes release a ready child to exit naturally.
            descriptor = os.open(release, os.O_WRONLY)
            os.close(descriptor)

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper._AsyncInput, "read", read)
    monkeypatch.setattr(session._events, "put", publish)
    monkeypatch.setattr(remote_helper, "emit", emit)
    writer.write(
        _start_session_command(
            [
                sys.executable,
                "-c",
                "import os,sys; print('ready',flush=True); "
                "fd=os.open(sys.argv[1],os.O_RDONLY); os.read(fd,1); os.close(fd)",
                str(release),
            ],
            ("ready",),
        )
    )

    async def run():
        tasks_before = asyncio.all_tasks()
        with pytest.raises(OSError if cleanup_fails else ValueError) as raised:
            await session.run_async()
        assert raised.value is (cleanup_error if cleanup_fails else failures[0])
        assert asyncio.all_tasks() == tasks_before

    try:
        asyncio.run(run())
    finally:
        for child in children:
            if child.process.returncode is None:
                asyncio.run(child.terminate())
            child.close_streams()
        session.workspace_lock.close()

    assert isinstance(failures[0], ValueError)
    assert session.cleanup_errors == ([cleanup_error] if cleanup_fails else []) + failures
    assert children[0].process.returncode == 0
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert not session.work.exists()
    assert session.workspace_lock.closed
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "ERROR"
    assert sum(event["type"] == "ERROR" for event in events) == 1
    assert not any(event["type"] == "SESSION_CLOSED" for event in events)


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


@pytest.mark.parametrize("failure_type", (RuntimeError, KeyboardInterrupt))
def test_spawn_return_interruption_keeps_child_reachable_for_cleanup(
    tmp_path, monkeypatch, control_pipe, failure_type
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = remote_helper.ControlSession.create()
    _reader, writer = control_pipe
    original_spawn = remote_helper._spawn_child
    children = []
    failure = failure_type("interrupted before coordinator adoption")

    def interrupt_return(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        raise failure

    monkeypatch.setattr(remote_helper, "_spawn_child", interrupt_return)
    writer.write(
        _start_session_command([sys.executable, "-c", "import signal;signal.pause()"], ("ready",))
    )

    async def run():
        tasks_before = asyncio.all_tasks()
        if failure_type is RuntimeError:
            await session.run_async()
            assert session.protocol_error is failure
        else:
            with pytest.raises(failure_type) as raised:
                await session.run_async()
            assert raised.value is failure
        assert asyncio.all_tasks() == tasks_before

    try:
        asyncio.run(run())
        assert len(children) == 1
        assert children[0].process.returncode is not None
        assert children[0].process.stdout.closed and children[0].process.stderr.closed
        assert not session.work.exists()
        assert session.workspace_lock.closed
    finally:
        for child in children:
            _cleanup_test_child(child, None)


def test_unconfirmed_spawn_rollback_retains_workspace_without_supervisor(
    tmp_path, monkeypatch, control_pipe
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = remote_helper.ControlSession.create()
    _reader, writer = control_pipe
    original_popen = remote_helper.subprocess.Popen
    original_waits = remote_helper._group_exit_waits
    processes = []
    failure = RuntimeError("supervisor construction failed")
    rollback_failure = PermissionError("group disposal could not be confirmed")

    def acquire(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail_supervisor(*_args, **_kwargs):
        raise failure

    def unconfirmed(pid):
        yield from original_waits(pid)
        raise rollback_failure

    monkeypatch.setattr(remote_helper.subprocess, "Popen", acquire)
    monkeypatch.setattr(remote_helper, "SupervisedChild", fail_supervisor)
    monkeypatch.setattr(remote_helper, "_group_exit_waits", unconfirmed)
    writer.write(
        _start_session_command([sys.executable, "-c", "import signal;signal.pause()"], ("ready",))
    )

    with pytest.raises(SystemExit) as raised:
        session.run()
    assert raised.value.code == 1
    assert session.protocol_error is failure
    assert any(str(rollback_failure) in note for note in failure.__notes__)
    assert processes[0].returncode is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed
    assert session.work.is_dir()
    assert session.workspace_lock.closed
    with pytest.raises(ValueError), remote_helper._stage_lease(session.work):
        pytest.fail("failed spawn rollback admitted staging")


def test_terminal_cleanup_closes_staging_before_waiting_for_child(
    tmp_path, monkeypatch, control_pipe
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = remote_helper.ControlSession.create()
    _reader, writer = control_pipe
    original_spawn = remote_helper._spawn_child
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        terminate = child.terminate

        async def cleanup():
            cleanup_started.set()
            await release_cleanup.wait()
            await terminate()

        monkeypatch.setattr(child, "terminate", cleanup)
        return child

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    writer.write(
        _start_session_command([sys.executable, "-c", "import signal;signal.pause()"], ("ready",))
        + b'{"version":1,"type":"STOP"}\n'
    )

    async def run():
        operation = asyncio.create_task(session.run_async())
        try:
            with remote_helper._stage_lease(session.work):
                await cleanup_started.wait()
                assert session.work.is_dir()
                with pytest.raises(ValueError), remote_helper._stage_lease(session.work):
                    pytest.fail("terminal cleanup admitted a new stage")
            release_cleanup.set()
            await operation
        finally:
            release_cleanup.set()
            await operation

    asyncio.run(run())
    assert not session.work.exists()
    assert session.workspace_lock.closed


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


@pytest.mark.parametrize("readiness", ("markers", "immediate"))
def test_readiness_reconciles_control_failure_waiting_for_publication(
    tmp_path, monkeypatch, control_pipe, readiness
):
    _reader, writer = control_pipe
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session = remote_helper.ControlSession.create()
    recognized = asyncio.Event()
    withheld = asyncio.Event()
    spawned = asyncio.Event()
    original_put = session._events.put
    original_get = session._events.get
    original_read = remote_helper._AsyncInput.read
    original_spawn = remote_helper._spawn_child
    children = []
    failures = []
    events = []

    async def publish(observation):
        if isinstance(observation, remote_helper._ObservationFailed):
            assert observation.source == "control"
            failures.append(observation.exception)
            recognized.set()
            await withheld.wait()
        await original_put(observation)

    async def receive():
        observation = await original_get()
        if readiness == "immediate" and isinstance(observation, remote_helper._ControlFrame):
            # The control reader can recognize the next frame's EOF failure
            # while dispatch of a valid START is still pending.
            await recognized.wait()
        return observation

    async def read(reader):
        if reader.descriptor == _reader.fileno():
            chunk = await original_read(reader)
            if not chunk and readiness == "markers":
                await spawned.wait()
            return chunk
        await recognized.wait()
        return await original_read(reader)

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        spawned.set()
        return child

    def emit(kind, **values):
        events.append((kind, values))
        if kind == "PROCESS_READY":
            # Let the pre-fix implementation shut down without a timeout.
            session.handle_signal()

    monkeypatch.setattr(session._events, "put", publish)
    monkeypatch.setattr(session._events, "get", receive)
    monkeypatch.setattr(remote_helper._AsyncInput, "read", read)
    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper, "emit", emit)
    writer.write(
        _start_session_command(
            [sys.executable, "-c", "import signal;print('ready',flush=True);signal.pause()"],
            ("ready",) if readiness == "markers" else (),
        )
        + b"{"
    )
    writer.close()

    async def run():
        tasks_before = asyncio.all_tasks()
        with suppress(ValueError):
            await session.run_async()
        assert asyncio.all_tasks() == tasks_before

    asyncio.run(run())
    assert failures and isinstance(failures[0], ValueError)
    assert not any(kind == "PROCESS_READY" for kind, _values in events)
    assert events[-1][0] == "ERROR"
    assert events[-1][1]["code"] == "PROTOCOL_ERROR"
    assert session.protocol_error is failures[0] and not session.cleanup_errors
    assert len(children) == 1 and children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert session.workspace_lock.closed and not any(tmp_path.iterdir())


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


@pytest.mark.parametrize("phase", ("active", "retry-fence"))
def test_cancelled_session_cleans_child_before_leaving_task_scope(
    tmp_path, monkeypatch, control_pipe, phase
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

    original_put = session._events.put
    coordinator_task: asyncio.Task[None] | None = None

    async def cancel_at_fence(observation):
        if isinstance(observation, remote_helper._ControlFence):
            assert coordinator_task is not None
            coordinator_task.cancel()
        await original_put(observation)

    if phase == "retry-fence":
        monkeypatch.setattr(session._events, "put", cancel_at_fence)
        writer.write(
            _start_session_command(
                [
                    sys.executable,
                    "-c",
                    "import sys;print('Address already in use',file=sys.stderr,flush=True)",
                ],
                ("not-ready",),
            )
        )
    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper, "emit", emit)
    if phase == "active":
        writer.write(_start_session_command([sys.executable, "-c", "import signal;signal.pause()"]))

    async def run():
        nonlocal coordinator_task
        coordinator_task = asyncio.current_task()
        tasks_before = asyncio.all_tasks()
        with pytest.raises(asyncio.CancelledError):
            await session.run_async()
        assert asyncio.all_tasks() == tasks_before

    asyncio.run(run())

    assert len(children) == 1
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
    assert not (workspace / remote_helper.UNCONFIRMED_CHILD).exists()
    assert lock.closed
    original_rmtree(workspace)


@pytest.mark.parametrize("observation", ("exists", "permission-denied"))
def test_control_session_unconfirmed_group_exit_fails_after_other_cleanup(
    tmp_path, monkeypatch, control_pipe, observation
):
    _reader, writer = control_pipe
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    session = remote_helper.ControlSession.create()
    children = []
    events = []
    original_spawn = remote_helper._spawn_child
    original_killpg = os.killpg
    original_sleep = asyncio.sleep
    now = 0.0

    def spawn(*args, **kwargs):
        child = original_spawn(*args, **kwargs)
        children.append(child)
        return child

    def killpg(pid, signum):
        if children and pid == children[0].pid and children[0].process.returncode is not None:
            assert signum == 0
            if observation == "permission-denied":
                raise PermissionError("group cannot be inspected")
            return
        original_killpg(pid, signum)

    async def controlled_sleep(delay):
        nonlocal now
        if (
            children
            and children[0].process.returncode is not None
            and 0 < delay <= remote_helper.CHILD_POLL_INTERVAL
        ):
            now += delay
        else:
            await original_sleep(delay)

    def emit(kind, **values):
        events.append((kind, values))
        if kind == "PROCESS_READY":
            writer.write(b'{"version":1,"type":"STOP"}\n')

    monkeypatch.setattr(remote_helper, "_spawn_child", spawn)
    monkeypatch.setattr(remote_helper.os, "killpg", killpg)
    monkeypatch.setattr(remote_helper, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(remote_helper.asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(remote_helper, "emit", emit)
    writer.write(
        _start_session_command(
            [sys.executable, "-c", "import signal;print('ready',flush=True);signal.pause()"],
            ("ready",),
        )
    )

    expected = TimeoutError if observation == "exists" else PermissionError
    with pytest.raises(expected):
        session.run()

    assert events[-1][0] == "ERROR"
    assert not any(kind == "SESSION_CLOSED" for kind, _ in events)
    assert children[0].process.returncode is not None
    assert children[0].process.stdout.closed and children[0].process.stderr.closed
    assert session.workspace_lock.closed
    assert session.work.is_dir()
    assert (session.work / remote_helper.UNCONFIRMED_CHILD).exists()
    with pytest.raises(ValueError), remote_helper._stage_lease(session.work):
        pytest.fail("retained workspace admitted staging")
    remote_helper.reclaim_stale_workspaces(
        session.work.parent, now=time.time() + remote_helper.STALE_SESSION_AGE + 1
    )
    assert session.work.is_dir()


@pytest.mark.parametrize("protocol_failure", (True, False))
def test_remote_cleanup_diagnostics_survive_error_serialization(
    tmp_path, monkeypatch, control_pipe, capsys, protocol_failure
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session = remote_helper.ControlSession.create()
    original_rmtree = remote_helper.shutil.rmtree
    original_close = session.workspace_lock.close
    workspace_failure = OSError("injected workspace removal failure")
    lock_failure = OSError("injected workspace lock closure failure")
    lock_detail = "workspace lock cleanup retained detail"
    lock_failure.add_note(lock_detail)

    def fail_workspace_removal(path):
        if path == session.work:
            raise workspace_failure
        original_rmtree(path)

    def fail_lock_close():
        original_close()
        raise lock_failure

    monkeypatch.setattr(remote_helper.ControlSession, "create", lambda: session)
    monkeypatch.setattr(remote_helper.shutil, "rmtree", fail_workspace_removal)
    monkeypatch.setattr(session.workspace_lock, "close", fail_lock_close)
    monkeypatch.setattr(remote_helper.sys, "argv", ["remote_helper.py", "control"])
    _reader, writer = control_pipe
    writer.write(b"not-json\n" if protocol_failure else b'{"version":1,"type":"STOP"}\n')

    try:
        with pytest.raises(SystemExit) as raised:
            remote_helper.main()

        assert raised.value.code == 1
        events = [decode_message(line) for line in capsys.readouterr().out.splitlines()]
        assert [event["type"] for event in events] == ["SESSION_CREATED", "ERROR"]
        assert events[-1]["code"] == ("PROTOCOL_ERROR" if protocol_failure else "HELPER_ERROR")
        primary = session.protocol_error if protocol_failure else workspace_failure
        assert events[-1]["message"].startswith(str(primary))
        assert str(workspace_failure) in events[-1]["message"]
        assert str(lock_failure) in events[-1]["message"]
        assert lock_detail in events[-1]["message"]
        assert session.work.exists()
        assert session.workspace_lock.closed
    finally:
        original_rmtree(session.work)
        original_close()


def test_protocol_error_retains_both_workspace_metadata_cleanup_failures(
    tmp_path, monkeypatch, control_pipe, capsys
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session = remote_helper.ControlSession.create()
    closure = remote_helper._closure_path(session.work)
    lease = remote_helper._lease_path(session.work)
    closure_failure = OSError("workspace closure removal failed")
    lease_failure = OSError("workspace lease removal failed")
    unlink = Path.unlink
    attempts = []

    def fail_metadata_removal(path, *args, **kwargs):
        if path in (closure, lease):
            attempts.append(path)
            raise closure_failure if path == closure else lease_failure
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_metadata_removal)
    monkeypatch.setattr(remote_helper.ControlSession, "create", lambda: session)
    _reader, writer = control_pipe
    writer.write(b"not-json\n")
    try:
        with pytest.raises(SystemExit) as raised:
            remote_helper.control()

        assert raised.value.code == 1
        events = [decode_message(line) for line in capsys.readouterr().out.splitlines()]
        assert [event["type"] for event in events] == ["SESSION_CREATED", "ERROR"]
        assert events[-1]["code"] == "PROTOCOL_ERROR"
        assert events[-1]["message"].startswith(str(session.protocol_error))
        assert str(closure_failure) in events[-1]["message"]
        assert str(lease_failure) in events[-1]["message"]
        assert attempts == [closure, lease]
        assert session.workspace_lock.closed
        assert not session.work.exists()
        assert closure.exists() and lease.exists()
    finally:
        session._release_workspace()
        unlink(closure, missing_ok=True)
        unlink(lease, missing_ok=True)


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
def cleanup_process():
    process = create_autospec(subprocess.Popen, instance=True)
    process.pid = 123
    process.returncode = None
    process.stdout = io.BytesIO()
    process.stderr = io.BytesIO()

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


@pytest.mark.parametrize("service_less", (False, True))
def test_control_session_does_not_launch_when_required_file_is_missing(
    tmp_path, monkeypatch, start_command, control_pipe, capsys, service_less
):
    workspace = tmp_path / "workspace"
    staged = workspace / "staged"
    staged.mkdir(parents=True)
    missing_file = staged / "image"
    start_command["required_paths"] = [
        {"kind": "file", "path": {"parts": [{"session": "workspace"}, "/staged/image"]}}
    ]
    spawn_calls = []
    monkeypatch.setattr(
        remote_helper,
        "_spawn_child",
        lambda *args, **kwargs: spawn_calls.append((args, kwargs)),
    )
    if service_less:
        start_command["services"] = []
    allocated_ports = []

    def allocate(ports, *, preferred_address=None):
        del preferred_address
        allocated_ports.append(tuple(ports))
        return "127.0.0.1"

    monkeypatch.setattr(remote_helper, "allocate_service_address", allocate)
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
    assert len(allocated_ports) == 1
    if service_less:
        assert allocated_ports == [()]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(event["type"] == "PROCESS_STARTING" for event in events)


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
        remote_helper.decode_command({"version": 1, "type": "STOP", "future": True})


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
    writer.write(b'{"version":1,"type":"STOP"}\n')
    with monkeypatch.context() as patch:
        patch.setattr(path_type, "unlink", fail_metadata_removal)
        with pytest.raises(PermissionError):
            session.run()

    assert not workspace.exists()
    assert owner_lock.closed
    assert set(tmp_path.iterdir()) == metadata
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "ERROR"
    assert not any(event["type"] == "SESSION_CLOSED" for event in events)

    for path in metadata:
        os.utime(path, (1.0, 1.0))
    remote_helper.reclaim_stale_workspaces(tmp_path, now=remote_helper.STALE_SESSION_AGE + 2)
    assert tuple(tmp_path.iterdir()) == ()


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
