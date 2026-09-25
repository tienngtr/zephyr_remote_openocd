# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import fcntl
import importlib.util
import io
import math
import os
import select
import signal
import sys
import threading
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
            child.terminate()
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        with suppress(BaseException):
            child.process.wait(timeout=5)
        with suppress(BaseException):
            child.dispose()
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
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


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

    remote_helper.relay(stream, "stdout", required_output_sentinels, captured)

    output_events = [values for _kind, values in events]
    assert [event["payload"] for event in output_events] == [
        "abc",
        "defg",
        "",
        "xyz",
        "vali",
        "d €",
        "inva",
        "lid ",
        "�tai",
        "l",
    ]
    assert [event["line_end"] for event in output_events] == [
        True,
        False,
        True,
        True,
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
    assert all(size == 4 for size in stream.read_sizes)
    assert not required_output_sentinels.ready
    assert [record.stream for record in captured] == ["stdout"] * len(captured)
    assert [record.line_end for record in captured] == [
        True,
        False,
        True,
        True,
        False,
        True,
        False,
        False,
        False,
        False,
    ]


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

    remote_helper.relay(stream, "stderr", required_output_sentinels, [])

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

    remote_helper.relay(
        _ChunkStream(first[:8].encode(), (first[8:] + "\n").encode()),
        "stdout",
        required_output_sentinels,
    )

    assert not required_output_sentinels.ready

    remote_helper.relay(
        _ChunkStream(("  " + second + "  \n").encode()),
        "stderr",
        required_output_sentinels,
    )

    assert required_output_sentinels.ready


def test_decode_start_accepts_full_output_lines_with_internal_spaces(start_command):
    start_command["required_output_sentinels"] = ["READY FOR START"]

    request = remote_helper.decode_command(start_command)

    assert request.required_output_sentinels == ("READY FOR START",)


def test_process_readiness_does_not_probe_service_sockets(monkeypatch, start_command):
    request = remote_helper.decode_command(start_command)
    required_output_sentinels = remote_helper._RequiredOutputSentinels(
        request.required_output_sentinels
    )
    required_output_sentinels.observe("READY")
    child = SimpleNamespace(
        pid=42,
        startup_output=[],
        required_output_sentinels=required_output_sentinels,
        poll=lambda: None,
        returncode=None,
    )
    events = []
    monkeypatch.setattr(remote_helper, "emit", lambda kind, **values: events.append((kind, values)))
    monkeypatch.setattr(
        remote_helper.socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("readiness must not probe service sockets"),
    )

    assert remote_helper._wait_for_process(child, "127.64.3.1", request, 0)
    assert [kind for kind, _values in events] == ["PROCESS_READY"]


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

    remote_helper.relay(_ChunkStream(payload), "stdout")

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

    remote_helper.relay(_ChunkStream(b"utf \xe2", b"\x82", b"\xac\ninvalid \xff"), "stderr")

    output_events = [values for _kind, values in events]
    assert (
        "".join(event["payload"] + ("\n" if event["line_end"] else "") for event in output_events)
        == "utf €\ninvalid �"
    )
    assert all(len(event["payload"]) <= 3 for event in output_events)


def test_relay_real_child_flushes_newline_free_output_before_exit(monkeypatch):
    relay_chunk_size = 64
    output_size = remote_helper.MAX_CAPTURED_STARTUP_FRAGMENTS * relay_chunk_size + 1
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", relay_chunk_size)
    events = []
    output_emitted = threading.Event()

    def emit(kind, **values):
        events.append((kind, values))
        output_emitted.set()

    monkeypatch.setattr(
        remote_helper,
        "emit",
        emit,
    )
    child = remote_helper._spawn_child(
        (
            sys.executable,
            "-c",
            "import signal,sys;"
            f"sys.stdout.buffer.write(b'x'*{output_size});sys.stdout.flush();signal.pause()",
        )
    )
    try:
        child.start_relays(capture_startup=True)
        assert output_emitted.wait(5)
        assert child.poll() is None
        assert all(len(values["payload"]) <= relay_chunk_size for _kind, values in events)
        assert len(child.startup_output) <= remote_helper.MAX_CAPTURED_STARTUP_FRAGMENTS
    finally:
        child.terminate()


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


def test_control_session_cleanup_attempts_all_resources_once(tmp_path):
    class Child:
        def __init__(self):
            self.terminate_calls = 0
            self.fail = True
            self.failure = RuntimeError("child cleanup failed")

        def terminate(self):
            self.terminate_calls += 1
            if self.fail:
                raise self.failure

    class Lock:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = Child()
    lock = Lock()
    session = remote_helper.ControlSession("session", workspace, lock)
    session.child = child

    with pytest.raises(RuntimeError) as raised:
        session.cleanup()

    assert raised.value is child.failure
    assert child.terminate_calls == 1
    assert lock.close_calls == 1
    assert not workspace.exists()
    assert session.stopping
    session.cleanup()
    assert child.terminate_calls == 1
    assert lock.close_calls == 1


def test_control_session_ignores_signal_while_cleaning(tmp_path):
    class Child:
        def __init__(self):
            self.terminate_calls = 0
            self.session = None
            self.reenter = True

        def terminate(self):
            self.terminate_calls += 1
            if self.reenter:
                self.reenter = False
                assert self.session is not None
                self.session.handle_signal()

    class Lock:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = Child()
    lock = Lock()
    session = remote_helper.ControlSession("session", workspace, lock)
    child.session = session
    session.child = child

    assert session.cleanup()

    assert child.terminate_calls == 1
    assert lock.close_calls == 1
    assert session.stopping
    assert not workspace.exists()
    assert not session.cleanup()


def test_control_session_natural_exit_cleans_before_close_event(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")

    class Child:
        returncode = SAMPLE_CHILD_EXIT_CODE

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

    events = []

    def record_event(kind, **values):
        if kind == "SESSION_CLOSED":
            assert not workspace.exists()
            assert lock.closed
        events.append((kind, values))

    monkeypatch.setattr(remote_helper, "emit", record_event)
    session = remote_helper.ControlSession("session", workspace, lock)
    session.child = Child()

    assert session._child_finished()
    assert events == [
        (
            "SESSION_CLOSED",
            {"reason": "process_exit", "returncode": SAMPLE_CHILD_EXIT_CODE},
        )
    ]


def test_control_session_natural_exit_cleanup_failure_emits_error_event(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")

    class Child:
        returncode = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

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

    class Selector:
        def register(self, *_args):
            pass

        def close(self):
            pass

    monkeypatch.setattr(remote_helper.shutil, "rmtree", fail_workspace_removal)
    monkeypatch.setattr(remote_helper.selectors, "DefaultSelector", Selector)
    session = remote_helper.ControlSession("session", workspace, lock)
    session.child = Child()

    helper_status = 0
    try:
        session.run()
    except Exception as exc:
        remote_helper.error(exc)
        helper_status = 1

    assert helper_status != 0
    assert [kind for kind, _values in events] == ["SESSION_CREATED", "ERROR"]
    assert isinstance(events[-1][1]["message"], str)
    assert events[-1][1]["message"]
    assert not any(kind == "SESSION_CLOSED" for kind, _values in events)
    assert workspace.exists()
    assert lock.closed

    monkeypatch.undo()
    original_rmtree(workspace)


def test_protocol_error_remains_primary_when_cleanup_also_fails(tmp_path, monkeypatch):
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

    class Selector:
        def register(self, *_args):
            pass

        def select(self, _timeout):
            return [(None, None)]

        def close(self):
            pass

    session = remote_helper.ControlSession("session", workspace, lock)
    monkeypatch.setattr(remote_helper.ControlSession, "create", lambda: session)
    monkeypatch.setattr(remote_helper.shutil, "rmtree", fail_workspace_removal)
    monkeypatch.setattr(remote_helper.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(remote_helper.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(remote_helper.sys, "argv", ["remote_helper.py", "control"])

    class Stdin:
        buffer = io.BytesIO(b"not-json\n")

    monkeypatch.setattr(remote_helper.sys, "stdin", Stdin())

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

        child.terminate()

        assert child.returncode == 0
        _assert_pidfd_exited(descendant_pidfd)
    finally:
        _cleanup_test_child(child, descendant_pidfd)
        if descendant_pidfd is not None:
            os.close(descendant_pidfd)


def test_supervised_child_warns_and_terminates_descendant_after_leader_exit(tmp_path, capsys):
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

        child.terminate()

        assert child.returncode == 0
        _assert_pidfd_exited(descendant_pidfd)
        assert str(descendant_pid) in capsys.readouterr().err
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

    child.terminate()

    assert signal.SIGTERM in signals
    assert signal.SIGKILL not in signals
    assert process.wait_calls
    assert all(timeout is not None for timeout in process.wait_calls)


def test_supervised_child_reaps_and_disposes_after_signal_errors(monkeypatch):
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
    disposed = []
    term_error = RuntimeError("term failed")

    def fail_term(_pid, signum):
        if signum == signal.SIGTERM:
            raise term_error
        raise ProcessLookupError

    def fail_dispose():
        disposed.append(True)
        raise RuntimeError("dispose failed")

    monkeypatch.setattr(remote_helper.os, "killpg", fail_term)
    monkeypatch.setattr(child, "dispose", fail_dispose)

    with pytest.raises(RuntimeError) as raised:
        child.terminate()

    assert raised.value is term_error
    assert process.wait_calls
    assert all(timeout is not None for timeout in process.wait_calls)
    assert disposed
    assert any("dispose failed" in note for note in raised.value.__notes__)


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
    monkeypatch.setattr(child, "_wait_for_leader_exit", lambda: True)
    monkeypatch.setattr(child, "_group_exists", lambda: True)
    monkeypatch.setattr(
        child,
        "_warn_remaining_group_members",
        lambda: (_ for _ in ()).throw(OSError("proc unavailable")),
    )

    child.terminate()

    assert signals.count(signal.SIGTERM) == 1
    assert signals.count(signal.SIGKILL) == 1
    assert signals.index(signal.SIGTERM) < signals.index(signal.SIGKILL)


def test_supervised_child_cleanup_uses_finite_budgets_after_failures(monkeypatch):
    class Clock:
        now = 10.0

        def monotonic(self):
            return self.now

    class Stream:
        closed = False

        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            self.closed = True

    class Process:
        pid = 123
        returncode = None

        def __init__(self):
            self.stdout = Stream()
            self.stderr = Stream()
            self.wait_calls = []

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            raise TimeoutError("reap failed")

    class Relay:
        ident = 1

        def __init__(self, name, *, consumes_budget=False):
            self.name = name
            self.alive = True
            self.join_calls = []
            self.consumes_budget = consumes_budget

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.join_calls.append(timeout)
            join_deadlines.append(clock.now + (timeout or 0.0))
            self.alive = False
            if self.consumes_budget:
                self.consumes_budget = False
                clock.now += remote_helper.CHILD_RELAY_JOIN_TIMEOUT

    clock = Clock()
    join_deadlines: list[float] = []
    process = Process()
    relays = [
        Relay("stdout-relay", consumes_budget=True),
        Relay("stderr-relay"),
    ]
    child = remote_helper.SupervisedChild(process)
    child.relay_threads = relays
    child._observed_returncode = 0
    signals = []
    signal_error = RuntimeError("signal failed")

    def fail_killpg(_pid, signum):
        signals.append(signum)
        if signum != 0:
            raise signal_error

    monkeypatch.setattr(remote_helper, "CHILD_RELAY_JOIN_TIMEOUT", 0.75)
    monkeypatch.setattr(remote_helper.os, "killpg", fail_killpg)
    monkeypatch.setattr(remote_helper.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(
        child,
        "_wait_for_leader_exit",
        lambda: (_ for _ in ()).throw(RuntimeError("leader wait failed")),
    )
    monkeypatch.setattr(child, "_warn_remaining_group_members", lambda: None)
    cleanup_deadline = clock.now + remote_helper.CHILD_RELAY_JOIN_TIMEOUT

    with pytest.raises(RuntimeError) as raised:
        child.terminate()

    assert raised.value is signal_error
    assert signals.count(signal.SIGTERM) == 1
    assert signals.count(signal.SIGKILL) == 1
    assert signals.index(signal.SIGTERM) < signals.index(signal.SIGKILL)
    assert process.wait_calls
    assert all(
        timeout is not None
        and math.isfinite(timeout)
        and 0 < timeout <= remote_helper.CHILD_REAP_TIMEOUT
        for timeout in process.wait_calls
    )
    join_calls = [timeout for relay in relays for timeout in relay.join_calls]
    assert join_calls
    assert all(0.0 <= timeout <= remote_helper.CHILD_RELAY_JOIN_TIMEOUT for timeout in join_calls)
    assert all(deadline <= cleanup_deadline for deadline in join_deadlines)
    assert process.stdout.closed and process.stdout.close_calls == 1
    assert process.stderr.closed and process.stderr.close_calls == 1
    assert any("leader wait failed" in note for note in raised.value.__notes__)
    assert any("reap failed" in note for note in raised.value.__notes__)


def test_supervised_child_wait_for_leader_exit_honors_deadline(monkeypatch):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

    process = Process()
    child = remote_helper.SupervisedChild(process)
    now = [100]
    waitid_calls = []
    sleeps = []

    def waitid(*args):
        waitid_calls.append(args)

    def sleep(duration):
        sleeps.append(duration)
        now[0] += duration

    monkeypatch.setattr(remote_helper, "CHILD_TERM_TIMEOUT", 3)
    monkeypatch.setattr(remote_helper, "CHILD_POLL_INTERVAL", 2)
    monkeypatch.setattr(remote_helper.os, "waitid", waitid)
    monkeypatch.setattr(remote_helper.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(remote_helper.time, "sleep", sleep)

    assert child._wait_for_leader_exit() is False
    assert sleeps
    assert all(0 < duration <= 2 for duration in sleeps)
    assert waitid_calls
    assert now[0] == 103


def test_decode_command_rejects_malformed_required_path_before_launch(start_command):
    start_command["required_paths"] = [{"kind": "socket", "path": "not-valid"}]
    with pytest.raises(ValueError, match="invalid required-path assertion"):
        remote_helper.decode_command(start_command)


def test_control_session_does_not_launch_when_required_file_is_missing(
    tmp_path, monkeypatch, start_command
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
    session = remote_helper.ControlSession("session", workspace, None)

    with pytest.raises(ValueError) as raised:
        session.dispatch(start_command)

    message = str(raised.value)
    assert "required remote file" in message
    assert "missing" in message
    assert str(missing_file) in message
    assert spawn_calls == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("argv", [], "argv"),
        ("environment", {"BAD=NAME": "value"}, "environment"),
        ("required_output_sentinels", [" READY "], "sentinels"),
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
