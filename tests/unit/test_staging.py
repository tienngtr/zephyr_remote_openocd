# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.controller.model import StagedDirectory, StagedFile
from zephyr_remote_openocd.controller.staging import StagingError, build_archive
from zephyr_remote_openocd.protocol.process import ProcessSpec

TEST_PROCESS = ProcessSpec(("test-process",))


class TestStaging:
    def test_build_archive_preserves_binary_and_empty_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "empty").write_bytes(b"")
            (root / "binary").write_bytes(bytes(range(256)) + b"\0")
            archive = build_archive(
                (
                    StagedFile(root / "empty", PurePosixPath("a/empty")),
                    StagedFile(root / "binary", PurePosixPath("b/binary")),
                ),
                spool_limit=1,
            )
            assert archive.byte_count == 257
            assert archive.sha256 == hashlib.sha256(bytes(range(256)) + b"\0").hexdigest()
            assert archive.directories == ()
            with tarfile.open(fileobj=archive.stream, mode="r:*") as packaged:
                assert packaged.getnames() == ["a/empty", "b/binary"]
                empty = packaged.extractfile("a/empty")
                binary = packaged.extractfile("b/binary")
                assert empty is not None and binary is not None
                assert empty.read() == b""
                assert binary.read() == bytes(range(256)) + b"\0"
            archive.stream.close()

    @pytest.mark.parametrize(
        "entries",
        (
            (
                StagedFile(Path("a"), PurePosixPath("root")),
                StagedFile(Path("b"), PurePosixPath("root/child")),
            ),
            (
                StagedDirectory(Path("a"), PurePosixPath("root")),
                StagedFile(Path("b"), PurePosixPath("root")),
            ),
        ),
        ids=("file-ancestor", "file-directory-duplicate"),
    )
    def test_build_archive_rejects_manifest_conflicts_before_reading_sources(self, entries):
        with pytest.raises(StagingError):
            build_archive(entries)
