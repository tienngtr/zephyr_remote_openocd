# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import errno
import io
import os
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import IO

import pytest
from zephyr_remote_openocd.helper import _workspace as workspace_module
from zephyr_remote_openocd.helper import staging as helper_staging


@pytest.mark.parametrize("storage", ("runtime", "cache"))
def test_workspace_uses_isolated_first_format_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str
) -> None:
    monkeypatch.setattr(workspace_module.Path, "home", lambda: tmp_path / "home")
    if storage == "runtime":
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        expected_root = tmp_path / "zephyr_remote_openocd-sessions-v1"
    else:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        expected_root = tmp_path / "home/.cache/zephyr_remote_openocd/sessions-v1"
    _session_id, workspace, lock = workspace_module.new_workspace()
    try:
        assert workspace_module.WORKSPACE_STORAGE_VERSION == 1
        assert workspace.parent == expected_root
        assert (workspace / "staged").is_dir()
        unowned = tmp_path / "unowned-session"
        unowned.mkdir()
        with pytest.raises(ValueError):
            helper_staging.stage(unowned)
    finally:
        try:
            workspace_module.remove_workspace(workspace)
        finally:
            lock.close()


def test_new_workspace_reclaims_only_unlocked_stale_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    _stale_id, stale, stale_lock = workspace_module.new_workspace()
    _active_id, active, active_lock = workspace_module.new_workspace()
    stale_lock.close()
    old = 1.0
    os.utime(stale, (old, old))
    os.utime(active, (old, old))

    _new_id, new, new_lock = workspace_module.new_workspace()
    try:
        assert not stale.exists()
        assert active.exists()
        assert new.exists()
    finally:
        active_lock.close()
        new_lock.close()


def test_reclaimer_removes_stale_directory_without_lock(tmp_path: Path) -> None:
    abandoned = tmp_path / "abandoned"
    abandoned.mkdir()
    os.utime(abandoned, (1.0, 1.0))

    workspace_module.reclaim_stale_workspaces(tmp_path, now=workspace_module.STALE_SESSION_AGE + 2)

    assert not abandoned.exists()


def test_reclaimer_continues_after_stale_lock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / workspace_module.SESSION_LOCK).touch()
    removable = tmp_path / "removable"
    removable.mkdir()
    (removable / workspace_module.SESSION_LOCK).touch()
    old = 1.0
    os.utime(blocked, (old, old))
    os.utime(removable, (old, old))

    original_flock = workspace_module.fcntl.flock

    def fail_blocked_lock(stream: IO[bytes], operation: int) -> None:
        if (
            Path(stream.name).name == workspace_module.SESSION_LOCK
            and Path(stream.name).parent.name == blocked.name
        ):
            raise PermissionError("injected stale-lock failure")
        return original_flock(stream, operation)

    monkeypatch.setattr(workspace_module.fcntl, "flock", fail_blocked_lock)
    workspace_module.reclaim_stale_workspaces(tmp_path, now=workspace_module.STALE_SESSION_AGE + 2)

    assert blocked.exists()
    assert not removable.exists()


def _fail_inspection(patch: pytest.MonkeyPatch, uncertain: Path, error_number: int) -> None:
    original_stat = os.stat
    original_lstat = os.lstat

    # Forward the patched call's remaining arguments unchanged.
    def inspect(
        operation: Callable[..., os.stat_result], path: object, *args: object, **kwargs: object
    ) -> os.stat_result:
        if path == uncertain or path == str(uncertain):
            raise OSError(error_number, "injected filesystem inspection failure")
        return operation(path, *args, **kwargs)

    patch.setattr(os, "stat", lambda *args, **kwargs: inspect(original_stat, *args, **kwargs))
    patch.setattr(os, "lstat", lambda *args, **kwargs: inspect(original_lstat, *args, **kwargs))


@pytest.mark.parametrize("source", ("marker", "lock", "metadata-workspace"))
@pytest.mark.parametrize("error_number", (errno.EACCES, errno.EIO))
def test_reclaimer_retains_entries_when_inspection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, error_number: int
) -> None:
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    _session_id, workspace, owner_lock = workspace_module.new_workspace()
    owner_lock.close()
    marker = workspace / workspace_module.UNCONFIRMED_CHILD
    if source == "marker":
        marker.touch()
        uncertain = marker
    elif source == "lock":
        uncertain = workspace / workspace_module.SESSION_LOCK
    else:
        uncertain = workspace
    metadata = (workspace_module.lease_path(workspace), workspace_module.closure_path(workspace))
    metadata[1].touch()
    inputs = workspace / "staged/child-input"
    inputs.write_bytes(b"retain uncertain inputs")
    for path in (workspace, *metadata):
        os.utime(path, (1.0, 1.0))

    with monkeypatch.context() as patch:
        _fail_inspection(patch, uncertain, error_number)
        workspace_module.reclaim_stale_workspaces(
            tmp_path, now=workspace_module.STALE_SESSION_AGE + 2
        )

    assert inputs.read_bytes() == b"retain uncertain inputs"
    assert all(path.is_file() for path in metadata)
    if source == "marker":
        assert marker.is_file()


def test_reclaimer_inspects_residual_marker_after_session_lock_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    _session_id, workspace, owner_lock = workspace_module.new_workspace()
    marker = workspace / workspace_module.UNCONFIRMED_CHILD
    os.utime(workspace, (1.0, 1.0))
    original_flock = workspace_module.fcntl.flock

    def finish_owner_before_lock_acquisition(stream: IO[bytes], operation: int) -> None:
        if Path(stream.name) == workspace / workspace_module.SESSION_LOCK:
            marker.touch()
            owner_lock.close()
        return original_flock(stream, operation)

    monkeypatch.setattr(workspace_module.fcntl, "flock", finish_owner_before_lock_acquisition)
    try:
        workspace_module.reclaim_stale_workspaces(
            tmp_path, now=workspace_module.STALE_SESSION_AGE + 2
        )
        assert owner_lock.closed
        assert marker.is_file()
        assert (workspace / "staged").is_dir()
    finally:
        owner_lock.close()


def test_new_workspace_removes_partial_directory_on_initialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    path_type = type(tmp_path)
    original_mkdir = path_type.mkdir
    failure = OSError("injected staging-directory failure")

    def fail_staging_directory(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path.name == "staged":
            raise failure
        original_mkdir(path, mode, parents, exist_ok)

    monkeypatch.setattr(path_type, "mkdir", fail_staging_directory)

    with pytest.raises(OSError) as raised:
        workspace_module.new_workspace()

    assert raised.value is failure
    assert tuple(tmp_path.iterdir()) == ()


def test_workspace_allocation_rollback_preserves_primary_and_cleans_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    original_mkdir = Path.mkdir
    original_open = Path.open
    locks = []
    primary = OSError("staged directory creation failed")
    secondary = KeyboardInterrupt("allocation lock close interrupted")
    secondary.add_note("lock rollback retained detail")

    def mkdir(path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False) -> None:
        if path.name == "staged":
            raise primary
        original_mkdir(path, mode, parents, exist_ok)

    # Workspace allocation opens its files with a binary mode only.
    def open_path(path: Path, mode: str) -> IO[bytes]:
        stream = original_open(path, mode)
        if path.name == workspace_module.SESSION_LOCK:
            locks.append(stream)
            close = stream.close

            def close_then_interrupt() -> None:
                close()
                raise secondary

            monkeypatch.setattr(stream, "close", close_then_interrupt)
        return stream

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(BaseException) as raised:
        workspace_module.new_workspace()
    assert raised.value is primary
    assert locks and all(lock.closed for lock in locks)
    assert not tuple(tmp_path.iterdir())
    assert any(str(secondary) in note for note in primary.__notes__)
    assert any(secondary.__notes__[0] in note for note in primary.__notes__)


@pytest.mark.parametrize("suffix", ("lease", "closed"))
def test_reclaimer_removes_orphaned_lease_but_preserves_live_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    _session_id, workspace, owner_lock = workspace_module.new_workspace()
    try:
        with workspace_module.stage_lease(workspace):
            orphan = tmp_path / f".removed.{suffix}"
            orphan.write_bytes(b"closed")
            live_lease = workspace_module.lease_path(workspace)
            for path in (orphan, live_lease):
                os.utime(path, (1.0, 1.0))
            workspace_module.reclaim_stale_workspaces(
                tmp_path, now=workspace_module.STALE_SESSION_AGE + 2
            )
            assert not orphan.exists()
            assert live_lease.exists()
            assert workspace.is_dir()
    finally:
        owner_lock.close()


def test_stage_rejects_workspace_named_through_missing_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, "workspace_root", lambda: tmp_path)
    _session_id, workspace, owner_lock = workspace_module.new_workspace()
    try:
        with pytest.raises(ValueError, match="active helper session"):
            helper_staging.stage(tmp_path / "ghost" / ".." / workspace.name)
    finally:
        try:
            workspace_module.remove_workspace(workspace)
        finally:
            owner_lock.close()


@pytest.mark.parametrize("error_number", (errno.EACCES, errno.EIO))
def test_closed_admission_rejects_stage_when_closure_inspection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_number: int,
) -> None:
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    _session_id, workspace, owner_lock = workspace_module.new_workspace()
    try:
        workspace_module.close_staging_admission(workspace)
        upload = io.BytesIO()
        with tarfile.open(fileobj=upload, mode="w") as archive:
            member = tarfile.TarInfo("late-upload.txt")
            member.size = len(b"late")
            archive.addfile(member, io.BytesIO(b"late"))
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(upload.getvalue())))
        # Failed inspection is not confirmed absence on any supported Python.
        with monkeypatch.context() as patch:
            _fail_inspection(patch, workspace_module.closure_path(workspace), error_number)
            with pytest.raises(OSError):
                helper_staging.stage(workspace)

        assert not tuple((workspace / "staged").iterdir())
        assert capsys.readouterr().out == ""
    finally:
        try:
            workspace_module.remove_workspace(workspace)
        finally:
            owner_lock.close()


def test_workspace_removal_retains_closure_when_workspace_inspection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_module, 'workspace_root', lambda: tmp_path)
    _session_id, workspace, owner_lock = workspace_module.new_workspace()
    owner_lock.close()
    closure = workspace_module.closure_path(workspace)

    def vanished_entry(_path: Path) -> None:
        raise FileNotFoundError("injected concurrent entry removal")

    with monkeypatch.context() as patch:
        patch.setattr(workspace_module.shutil, "rmtree", vanished_entry)
        _fail_inspection(patch, workspace, errno.EIO)
        with pytest.raises(OSError) as raised:
            workspace_module.remove_workspace(workspace)

    assert not isinstance(raised.value, FileNotFoundError)
    assert workspace.is_dir()
    assert closure.is_file()
    workspace_module.remove_workspace(workspace)
    assert tuple(tmp_path.iterdir()) == ()
