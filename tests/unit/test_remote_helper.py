# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import fcntl
import importlib.util
import io
import os
import signal
import sys
import threading
import time
from contextlib import suppress

import pytest

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "zro_remote_helper", ROOT / "python/zephyr_remote_openocd/remote_helper.py"
)
assert SPEC is not None and SPEC.loader is not None
remote_helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remote_helper)


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


def _assert_pid_gone(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    raise AssertionError(f"descendant process {pid} survived cleanup")


def _cleanup_test_child(child, descendant_pid):
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
    if descendant_pid is not None:
        with suppress(ProcessLookupError):
            os.kill(descendant_pid, signal.SIGKILL)


def _forking_child_code(exit_on_term):
    leader_exit = (
        """
def stop(_signal, _frame):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
"""
        if exit_on_term
        else ""
    )
    parent_wait = "time.sleep(30)" if exit_on_term else "time.sleep(0.05)"
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
        "readiness_marker": "READY",
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
    marker_seen = threading.Event()
    captured = []
    stream = _ChunkStream(
        b"abc\n",
        b"defg",
        b"\nxy",
        b"z\nvalid \xe2",
        b"\x82\xac\ninvalid \xff",
        b"tail",
    )

    remote_helper.relay(stream, "stdout", "READY", marker_seen, captured)

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
    assert not marker_seen.is_set()
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


def test_relay_matches_marker_only_after_complete_line(monkeypatch):
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 8)
    events = []
    monkeypatch.setattr(
        remote_helper,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )
    marker_seen = threading.Event()
    stream = _ChunkStream(b"NO", b"\nREA", b"DY", b"\n")

    remote_helper.relay(stream, "stderr", "READY", marker_seen, [])

    assert marker_seen.is_set()
    assert [values["payload"] for _kind, values in events] == ["NO", "READY"]
    assert [values["line_end"] for _kind, values in events] == [True, True]


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
    monkeypatch.setattr(remote_helper, "RELAY_CHUNK_SIZE", 64)
    events = []
    monkeypatch.setattr(
        remote_helper,
        "emit",
        lambda kind, **values: events.append((kind, values)),
    )
    child = remote_helper._spawn_child(
        (
            sys.executable,
            "-c",
            "import sys,time;sys.stdout.buffer.write(b'x'*8193);sys.stdout.flush();time.sleep(5)",
        )
    )
    try:
        child.start_relays(capture_startup=True)
        deadline = time.monotonic() + 2
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert events
        assert child.poll() is None
        assert all(len(values["payload"]) <= 64 for _kind, values in events)
        assert len(child.startup_output) <= 128
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

    def fail_staging_directory(path, *args, **kwargs):
        if path.name == "staged":
            raise OSError("injected staging-directory failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "mkdir", fail_staging_directory)

    with pytest.raises(OSError, match="injected staging-directory failure"):
        remote_helper.new_workspace()

    assert tuple(tmp_path.iterdir()) == ()


def test_control_session_cleans_up_when_announcement_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session_id, workspace, lock = remote_helper.new_workspace()

    def fail_announce(_session):
        raise BrokenPipeError("injected announcement failure")

    monkeypatch.setattr(remote_helper.ControlSession, "announce", fail_announce)
    session = remote_helper.ControlSession(session_id, workspace, lock)

    with pytest.raises(BrokenPipeError, match="injected announcement failure"):
        session.run()

    assert not workspace.exists()
    assert lock.closed


def test_control_session_cleanup_attempts_all_resources_once(tmp_path):
    class Child:
        def __init__(self):
            self.terminate_calls = 0
            self.fail = True

        def terminate(self):
            self.terminate_calls += 1
            if self.fail:
                raise RuntimeError("child cleanup failed")

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

    with pytest.raises(RuntimeError, match="child cleanup failed"):
        session.cleanup()

    assert child.terminate_calls == 1
    assert lock.close_calls == 1
    assert not workspace.exists()
    assert session.stopping
    session.cleanup()
    assert child.terminate_calls == 1
    assert lock.close_calls == 1


def test_control_session_natural_exit_cleans_before_close_event(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock = (workspace / remote_helper.SESSION_LOCK).open("w+b")

    class Child:
        returncode = 7

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
    assert events == [("SESSION_CLOSED", {"reason": "process_exit", "returncode": 7})]


def test_control_session_natural_exit_cleanup_failure_is_terminal_error(tmp_path, monkeypatch):
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
    assert events[-1][1]["message"] == "injected workspace removal failure"
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
    assert "Expecting value" in events[-1][1]["message"]
    assert any("session cleanup also failed" in note for note in session.protocol_error.__notes__)
    assert workspace.exists()
    assert lock.closed

    monkeypatch.undo()
    original_rmtree(workspace)


def test_supervised_child_terminates_descendant_after_leader_term(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "CHILD_TERM_TIMEOUT", 0.2)
    descendant_path = tmp_path / "descendant.pid"
    child = remote_helper._spawn_child(
        (sys.executable, "-c", _forking_child_code(True), str(descendant_path))
    )
    descendant_pid = None
    try:
        descendant_pid = _wait_for_descendant(descendant_path)
        assert os.getpgid(descendant_pid) == child.pid
        assert child.poll() is None

        child.terminate()

        assert child.returncode == 0
        _assert_pid_gone(descendant_pid)
    finally:
        _cleanup_test_child(child, descendant_pid)


def test_supervised_child_warns_and_terminates_descendant_after_leader_exit(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(remote_helper, "CHILD_TERM_TIMEOUT", 0.2)
    descendant_path = tmp_path / "descendant.pid"
    child = remote_helper._spawn_child(
        (sys.executable, "-c", _forking_child_code(False), str(descendant_path))
    )
    descendant_pid = None
    try:
        descendant_pid = _wait_for_descendant(descendant_path)
        assert os.getpgid(descendant_pid) == child.pid
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child.returncode == 0

        child.terminate()

        _assert_pid_gone(descendant_pid)
        assert str(descendant_pid) in capsys.readouterr().err
    finally:
        _cleanup_test_child(child, descendant_pid)


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

    def fail_term(_pid, signum):
        if signum == signal.SIGTERM:
            raise RuntimeError("term failed")
        raise ProcessLookupError

    def fail_dispose():
        disposed.append(True)
        raise RuntimeError("dispose failed")

    monkeypatch.setattr(remote_helper.os, "killpg", fail_term)
    monkeypatch.setattr(child, "dispose", fail_dispose)

    with pytest.raises(RuntimeError, match="term failed") as raised:
        child.terminate()

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

    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_supervised_child_cleanup_is_bounded_when_relays_remain_live(monkeypatch):
    monkeypatch.setattr(remote_helper, "emit", lambda *_args, **_values: None)
    child = remote_helper._spawn_child((sys.executable, "-c", "import time; time.sleep(30)"))
    child.start_relays()
    original_killpg = remote_helper.os.killpg

    def fail_killpg(_pid, _signum):
        raise RuntimeError("signal failed")

    monkeypatch.setattr(remote_helper.os, "killpg", fail_killpg)
    monkeypatch.setattr(
        child,
        "_wait_for_leader_exit",
        lambda: (_ for _ in ()).throw(RuntimeError("leader wait failed")),
    )
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="signal failed") as raised:
            child.terminate()

        assert time.monotonic() - started < 6
        assert any("relay did not stop" in note for note in raised.value.__notes__)
    finally:
        with suppress(ProcessLookupError):
            original_killpg(child.pid, signal.SIGKILL)
        with suppress(BaseException):
            child.process.wait(timeout=5)
        with suppress(BaseException):
            child.dispose()


def test_decode_command_rejects_malformed_required_path_before_launch(start_command):
    start_command["required_paths"] = [{"kind": "socket", "path": "not-valid"}]
    with pytest.raises(ValueError, match="invalid required-path assertion"):
        remote_helper.decode_command(start_command)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("argv", [], "argv"),
        ("environment", {"BAD=NAME": "value"}, "environment"),
        ("readiness_marker", "not a token", "marker"),
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
