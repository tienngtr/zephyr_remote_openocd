# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import fcntl
import importlib.util
import os

import pytest

from tests.support import ROOT

SPEC = importlib.util.spec_from_file_location(
    "zro_remote_helper", ROOT / "python/zephyr_remote_openocd/remote_helper.py"
)
assert SPEC is not None and SPEC.loader is not None
remote_helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remote_helper)


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


def test_control_session_cleans_up_when_selector_creation_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session_id, workspace, lock = remote_helper.new_workspace()

    def fail_selector():
        raise OSError("injected selector creation failure")

    monkeypatch.setattr(remote_helper.selectors, "DefaultSelector", fail_selector)
    session = remote_helper.ControlSession(session_id, workspace, lock)

    with pytest.raises(OSError, match="injected selector creation failure"):
        session.run()

    assert not workspace.exists()
    assert lock.closed


def test_control_session_closes_selector_and_cleans_up_on_registration_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(remote_helper, "workspace_root", lambda: tmp_path)
    session_id, workspace, lock = remote_helper.new_workspace()
    selectors_created = []

    class FailingSelector:
        def __init__(self):
            self.closed = False
            selectors_created.append(self)

        def register(self, *_args):
            raise OSError("injected selector registration failure")

        def close(self):
            self.closed = True

    monkeypatch.setattr(remote_helper.selectors, "DefaultSelector", FailingSelector)
    session = remote_helper.ControlSession(session_id, workspace, lock)

    with pytest.raises(OSError, match="injected selector registration failure"):
        session.run()

    assert selectors_created[0].closed
    assert not workspace.exists()
    assert lock.closed


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
