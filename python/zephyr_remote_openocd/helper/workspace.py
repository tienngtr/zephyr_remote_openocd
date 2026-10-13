# SPDX-License-Identifier: Apache-2.0

"""Remote workspace allocation, staging admission, and cleanup ownership."""

from __future__ import annotations

import fcntl
import os
import secrets
import shutil
import stat
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

from zephyr_remote_openocd.helper.cleanup import _raise_cleanup_errors

SESSION_LOCK = ".session.lock"
UNCONFIRMED_CHILD = ".child-disposal-unconfirmed"
WORKSPACE_STORAGE_VERSION = 1
STALE_SESSION_AGE = 24 * 60 * 60
WORKSPACE_LEASE_TIMEOUT = 5
WORKSPACE_LEASE_POLL_INTERVAL = 0.05
MAX_SESSION_ID_ATTEMPTS = 32
SESSION_ID_RANDOM_BYTES = 18


def workspace_root():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / f"zephyr_remote_openocd-sessions-v{WORKSPACE_STORAGE_VERSION}"
    return (
        Path.home() / ".cache" / "zephyr_remote_openocd" / f"sessions-v{WORKSPACE_STORAGE_VERSION}"
    )


def _lease_path(work: Path) -> Path:
    return work.parent / f".{work.name}.lease"


def _closure_path(work: Path) -> Path:
    return work.parent / f".{work.name}.closed"


@contextmanager
def _stage_lease(work: Path) -> Generator[None, None, None]:
    # Acquire ownership before checking closure. If cleanup wins before this
    # check, reject; if it wins afterwards, our shared lease prevents removal.
    # Allocation creates the lease. A delayed upload must not recreate metadata
    # after successful cleanup has removed the workspace and its admission state.
    with _lease_path(work).open("r+b") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace cleanup has begun") from None
        if not _path_absent(_closure_path(work)) or not work.is_dir():
            raise ValueError("workspace is not an active helper session")
        yield


def remove_workspace(work: Path) -> None:
    """Close admission before waiting for all admitted stages to release ownership."""
    # O_CREAT publishes closure atomically without depending on any lock owner.
    # Keep the lease identity and closed admission until workspace removal
    # succeeds; delayed stages reject missing workspaces even on an old inode.
    close_staging_admission(work)
    with _lease_path(work).open("a+b") as lease:
        deadline = time.monotonic() + WORKSPACE_LEASE_TIMEOUT
        while True:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("staging did not release its workspace lease") from None
                time.sleep(WORKSPACE_LEASE_POLL_INTERVAL)
        try:
            shutil.rmtree(work)
        except FileNotFoundError:
            if not _path_absent(work):
                raise
        errors = []
        for metadata in (_closure_path(work), _lease_path(work)):
            try:
                metadata.unlink(missing_ok=True)
            except BaseException as exc:
                errors.append(exc)
        _raise_cleanup_errors(errors)


def close_staging_admission(work: Path) -> None:
    """Publish closure independently of child cleanup or an active upload."""
    with _closure_path(work).open("ab"):
        pass


def _reclaim_unmarked_workspace(work: Path) -> None:
    """Require confirmed marker absence; inspection errors retain the inputs."""
    try:
        (work / UNCONFIRMED_CHILD).lstat()
    except FileNotFoundError:
        remove_workspace(work)


def reclaim_stale_workspaces(root, now=None):
    """Reclaim only entries with established inactive ownership and disposal."""
    cutoff = (time.time() if now is None else now) - STALE_SESSION_AGE
    try:
        candidates = tuple(root.iterdir())
    except OSError:
        return
    for path in candidates:
        lock_path = path / SESSION_LOCK
        try:
            entry = path.lstat()
            if entry.st_mtime > cutoff:
                continue
            if stat.S_ISREG(entry.st_mode) and path.name.startswith("."):
                for suffix in (".lease", ".closed"):
                    if path.name.endswith(suffix):
                        # Metadata is retired only after its workspace is gone.
                        # A pending stage still checks closure and workspace
                        # existence after locking, including on a retired inode.
                        try:
                            (root / path.name[1 : -len(suffix)]).lstat()
                        except FileNotFoundError:
                            path.unlink()
                        break
                continue
            if not stat.S_ISDIR(entry.st_mode):
                continue
            try:
                lock_entry = lock_path.lstat()
            except FileNotFoundError:
                _reclaim_unmarked_workspace(path)
                continue
            if not stat.S_ISREG(lock_entry.st_mode):
                continue
            lock = lock_path.open("r+b")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            with suppress(OSError):
                # Inspect after acquiring the session lock: the previous
                # owner may publish a residual marker before releasing it.
                _reclaim_unmarked_workspace(path)
        finally:
            lock.close()


def new_workspace():
    root = workspace_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    reclaim_stale_workspaces(root)
    for _ in range(MAX_SESSION_ID_ATTEMPTS):
        session_id = secrets.token_urlsafe(SESSION_ID_RANDOM_BYTES)
        path = root / session_id
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        lock = None
        try:
            lock = (path / SESSION_LOCK).open("xb")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (path / "staged").mkdir(mode=0o700)
            with _lease_path(path).open("xb"):
                pass
            return session_id, path, lock
        except BaseException as failure:
            cleanups = [lambda work=path: remove_workspace(work)]
            if lock is not None:
                cleanups.append(lock.close)
            for cleanup in cleanups:
                try:
                    cleanup()
                except BaseException as cleanup_error:
                    failure.add_note(f"workspace allocation rollback also failed: {cleanup_error}")
                    for note in getattr(cleanup_error, "__notes__", ()):
                        failure.add_note(f"workspace allocation rollback detail: {note}")
            raise
    raise RuntimeError("could not allocate an unpredictable session directory")


def _path_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False
