# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.controller import staging as staging_module
from zephyr_remote_openocd.controller.staging import StagingError, build_archive
from zephyr_remote_openocd.controller.staging_spec import StagedDirectory, StagedEntry, StagedFile


class TestStaging:
    def test_build_archive_preserves_binary_and_empty_files(self) -> None:
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
        "destinations",
        (
            ((StagedFile, "root"), (StagedFile, "root/child")),
            ((StagedFile, "root/child"), (StagedFile, "root")),
            ((StagedDirectory, "root"), (StagedFile, "root")),
        ),
        ids=("file-ancestor", "descendant-first", "file-directory-duplicate"),
    )
    def test_build_archive_rejects_manifest_conflicts(
        self, tmp_path: Path, destinations: tuple[tuple[type[StagedEntry], str], ...]
    ) -> None:
        # Readable sources leave the manifest conflict as the only failure.
        (tmp_path / "file").write_bytes(b"content")
        entries = tuple(
            kind(tmp_path / "file" if kind is StagedFile else tmp_path, PurePosixPath(destination))
            for kind, destination in destinations
        )
        with pytest.raises(StagingError):
            build_archive(entries)


def test_archive_build_failure_preserves_secondary_close_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        tempfile.SpooledTemporaryFile(max_size=1, mode="w+b") as stream,
        monkeypatch.context() as patch,
    ):
        close = stream.close
        cleanup = OSError("archive close failed")
        cleanup.add_note("retained close diagnostic")

        def fail_close() -> None:
            close()
            raise cleanup

        patch.setattr(stream, "close", fail_close)
        patch.setattr(staging_module.tempfile, "SpooledTemporaryFile", lambda **_kwargs: stream)
        with pytest.raises(StagingError) as raised:
            build_archive((StagedFile(tmp_path / "missing", PurePosixPath("firmware.bin")),))

        assert isinstance(raised.value.__cause__, FileNotFoundError)
        assert stream.closed
        notes = getattr(raised.value, "__notes__", ())
        assert any(str(cleanup) in note for note in notes)
        assert any("retained close diagnostic" in note for note in notes)
