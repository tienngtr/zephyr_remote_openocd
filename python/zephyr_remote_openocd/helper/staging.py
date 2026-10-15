# SPDX-License-Identifier: Apache-2.0

"""Safe extraction into leased remote workspaces."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal, NamedTuple

from zephyr_remote_openocd.helper import _streams as streams
from zephyr_remote_openocd.helper import _workspace as workspace


def valid_member(member, seen):
    name = member.name
    if member.isdir() and name.endswith("/"):
        name = name[:-1]
    parts = name.split("/") if name else []
    if not name or name == "." or any(p in ("", ".", "..") for p in parts) or "\0" in name:
        raise ValueError(f"unsafe archive path: {member.name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name:
        raise ValueError(f"unsafe archive path: {member.name!r}")
    if path in seen:
        raise ValueError(f"duplicate archive path: {path}")
    seen.add(path)
    if member.isdir():
        if member.size:
            raise ValueError(f"archive directory has content: {path}")
    elif not member.isreg():
        raise ValueError(f"archive member is not a regular file or directory: {path}")
    return path


class _ValidatedArchiveMember(NamedTuple):
    member: tarfile.TarInfo
    relative: PurePosixPath
    target: Path
    kind: Literal["directory", "file"]


class _StagingResult(NamedTuple):
    byte_count: int
    sha256: str
    files: tuple[str, ...]
    directories: tuple[str, ...]


def _validate_archive(
    archive: tarfile.TarFile, target_root: Path
) -> tuple[_ValidatedArchiveMember, ...]:
    """Validate the complete archive before any extraction can mutate its targets."""
    members = archive.getmembers()
    seen: set[PurePosixPath] = set()
    validated = []
    kinds = {}
    for member in members:
        relative = valid_member(member, seen)
        kind: Literal["directory", "file"] = "directory" if member.isdir() else "file"
        kinds[relative] = kind
        target = target_root.joinpath(*relative.parts)
        if target_root.resolve() not in target.resolve().parents:
            raise ValueError(f"archive path escapes staging directory: {relative}")
        validated.append(_ValidatedArchiveMember(member, relative, target, kind))
    if any(
        kind == "file" and any(path in other.parents for other in kinds)
        for path, kind in kinds.items()
    ):
        raise ValueError("archive contains a file/directory ancestor conflict")
    return tuple(validated)


def _extract_archive(
    archive: tarfile.TarFile, validated: tuple[_ValidatedArchiveMember, ...]
) -> _StagingResult:
    """Extract an already-validated manifest and summarize regular-file content."""
    count = 0
    digest = hashlib.sha256()
    names = []
    for member, relative, target, kind in validated:
        if kind == "directory":
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Keep extracted directories owner-private and writable so
            # session cleanup can remove their contents regardless of
            # archive permission metadata.
            os.chmod(target, 0o700)
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise ValueError(f"missing archive content: {relative}")
        with target.open("wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                count += len(chunk)
                digest.update(chunk)
        os.chmod(target, member.mode & 0o700 or 0o600)
        names.append(str(relative))
    return _StagingResult(
        count,
        digest.hexdigest(),
        tuple(names),
        tuple(str(relative) for _, relative, _, kind in validated if kind == "directory"),
    )


def stage(workspace_path: str | os.PathLike[str]) -> None:
    root = workspace.workspace_root().resolve()
    work = Path(workspace_path).resolve()
    if root not in work.parents or work.parent != root or not work.is_dir():
        raise ValueError("workspace is not an active helper session")
    target_root = work / "staged"
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        shutil.copyfileobj(sys.stdin.buffer, spool, length=1024 * 1024)
        with workspace.stage_lease(work):
            spool.seek(0)
            with tarfile.open(fileobj=spool, mode="r:*") as archive:
                validated = _validate_archive(archive, target_root)
                result = _extract_archive(archive, validated)
            streams.emit(
                "STAGED",
                byte_count=result.byte_count,
                sha256=result.sha256,
                files=list(result.files),
                directories=list(result.directories),
            )
    finally:
        spool.close()
