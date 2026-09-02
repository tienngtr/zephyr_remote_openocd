"""Validated, bounded-buffer POSIX tar staging."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from typing import BinaryIO, Iterable

from .model import StagedFile, validated_destination


class StagingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveInfo:
    stream: BinaryIO
    byte_count: int
    sha256: str
    files: tuple[str, ...]


def build_archive(files: Iterable[StagedFile], *, spool_limit: int = 1024 * 1024) -> ArchiveInfo:
    manifest = tuple(files)
    destinations = [str(item.destination) for item in manifest]
    if len(destinations) != len(set(destinations)):
        raise StagingError("duplicate staged destination")
    stream = tempfile.SpooledTemporaryFile(max_size=spool_limit, mode="w+b")
    try:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for item in manifest:
                source = item.source
                try:
                    status = source.stat()
                    if not source.is_file():
                        raise StagingError(f"staged source is not a regular file: {source}")
                    with source.open("rb") as content:
                        info = tarfile.TarInfo(str(item.destination))
                        info.size = status.st_size
                        info.mode = status.st_mode & 0o777
                        info.mtime = int(status.st_mtime)
                        archive.addfile(info, content)
                except OSError as error:
                    raise StagingError(f"cannot read staged source {source}: {error}") from error
        stream.seek(0)
        digest = hashlib.sha256()
        size = 0
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        stream.seek(0)
        return ArchiveInfo(stream, size, digest.hexdigest(), tuple(destinations))
    except BaseException:
        stream.close()
        raise


def extract_archive(stream: BinaryIO, destination: Path) -> tuple[int, str, tuple[str, ...]]:
    """Extract regular files after explicit checks; safe on Python 3.12."""
    destination = destination.resolve()
    seen: set[PurePosixPath] = set()
    count = 0
    digest = hashlib.sha256()
    names: list[str] = []
    with tarfile.open(fileobj=stream, mode="r:*") as archive:
        members = archive.getmembers()
        for member in members:
            try:
                relative = validated_destination(member.name)
            except ValueError as error:
                raise StagingError(str(error)) from error
            if relative in seen:
                raise StagingError(f"duplicate archive destination: {relative}")
            seen.add(relative)
            if not member.isreg():
                raise StagingError(f"archive member is not a regular file: {relative}")
            target = destination.joinpath(*relative.parts)
            if destination not in target.resolve().parents:
                raise StagingError(f"archive member escapes staging directory: {relative}")
        for member in members:
            relative = PurePosixPath(member.name)
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise StagingError(f"archive member has no content: {relative}")
            with target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    count += len(chunk)
                    digest.update(chunk)
            os.chmod(target, member.mode & 0o700 or 0o600)
            names.append(str(relative))
    return count, digest.hexdigest(), tuple(names)
