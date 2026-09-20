# SPDX-License-Identifier: Apache-2.0

"""Validated, bounded-buffer POSIX tar staging."""

from __future__ import annotations

import hashlib
import tarfile
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import BinaryIO, cast

from .model import StagedDirectory, StagedEntry, StagedFile


class StagingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveInfo:
    stream: BinaryIO
    byte_count: int
    sha256: str
    files: tuple[str, ...]
    directories: tuple[str, ...]


class _DigestingReader:
    """Hash file bytes as tarfile consumes them without buffering the file."""

    def __init__(self, source: BinaryIO, digest) -> None:
        self.source = source
        self.digest = digest
        self.byte_count = 0

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        self.byte_count += len(data)
        self.digest.update(data)
        return data


def _validate_manifest(
    manifest: tuple[StagedEntry, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    paths = {}
    for item in manifest:
        if not isinstance(item, (StagedFile, StagedDirectory)):
            raise StagingError("staged manifest contains an unsupported entry")
        destination = item.destination
        if destination in paths:
            raise StagingError("duplicate staged destination")
        paths[destination] = "directory" if isinstance(item, StagedDirectory) else "file"
    for path, kind in paths.items():
        if kind == "file" and any(path in other.parents for other in paths):
            raise StagingError(f"staged file ancestor conflict: {path}")
    return (
        tuple(str(item.destination) for item in manifest if isinstance(item, StagedFile)),
        tuple(str(item.destination) for item in manifest if isinstance(item, StagedDirectory)),
    )


def build_archive(files: Iterable[StagedEntry], *, spool_limit: int = 1024 * 1024) -> ArchiveInfo:
    manifest = tuple(files)
    file_names, directory_names = _validate_manifest(manifest)
    stream = cast(
        BinaryIO,
        tempfile.SpooledTemporaryFile(max_size=spool_limit, mode="w+b"),  # noqa: SIM115
    )
    try:
        digest = hashlib.sha256()
        size = 0
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for item in manifest:
                source = item.source
                try:
                    status = source.stat()
                    info = tarfile.TarInfo(str(item.destination))
                    info.mode = status.st_mode & 0o777
                    info.mtime = int(status.st_mtime)
                    if isinstance(item, StagedDirectory):
                        if not source.is_dir():
                            raise StagingError(f"staged source is not a directory: {source}")
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    else:
                        if not source.is_file():
                            raise StagingError(f"staged source is not a regular file: {source}")
                        info.size = status.st_size
                        with source.open("rb") as content:
                            reader = _DigestingReader(content, digest)
                            archive.addfile(info, reader)
                            size += reader.byte_count
                except OSError as error:
                    raise StagingError(f"cannot read staged source {source}: {error}") from error
        stream.seek(0)
        return ArchiveInfo(stream, size, digest.hexdigest(), file_names, directory_names)
    except BaseException:
        stream.close()
        raise
