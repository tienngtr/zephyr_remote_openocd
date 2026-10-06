# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
from tempfile import NamedTemporaryFile
from types import TracebackType
from typing import IO, NoReturn

import pytest
from zephyr_remote_openocd.zephyr44 import runner_state
from zephyr_remote_openocd.zephyr44.runner_state import update_runner_default


def test_failed_metadata_replacement_preserves_state_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    metadata = tmp_path / "runners.yaml"
    before = "flash-runner: openocd\ndebug-runner: openocd\n"
    metadata.write_text(before)

    def reject_replace(_source: Path, _target: Path) -> NoReturn:
        raise OSError("replacement failed")

    monkeypatch.setattr(Path, "replace", reject_replace)

    with pytest.raises(OSError):
        update_runner_default(metadata, "remote_openocd")

    assert metadata.read_text() == before
    assert list(tmp_path.iterdir()) == [metadata]


def test_metadata_refresh_preserves_file_permissions(tmp_path: Path):
    metadata = tmp_path / "runners.yaml"
    metadata.write_text("flash-runner: openocd\ndebug-runner: openocd\n")
    metadata.chmod(0o640)
    mode = metadata.stat().st_mode

    update_runner_default(metadata, "remote_openocd")

    assert metadata.stat().st_mode == mode


def test_failed_metadata_close_preserves_state_and_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    metadata = tmp_path / "runners.yaml"
    before = "flash-runner: openocd\ndebug-runner: openocd\n"
    metadata.write_text(before)
    with NamedTemporaryFile(mode="w", dir=tmp_path, delete=False) as stream:

        class FailingClose:
            name = stream.name

            def __enter__(self) -> IO[str]:
                return stream.file

            def __exit__(
                self,
                _exc_type: type[BaseException] | None,
                _exc: BaseException | None,
                _traceback: TracebackType | None,
            ) -> NoReturn:
                stream.close()
                raise OSError("close failed")

        def temporary_file(**_kwargs: object) -> FailingClose:
            return FailingClose()

        monkeypatch.setattr(runner_state, "NamedTemporaryFile", temporary_file)

        with pytest.raises(OSError):
            update_runner_default(metadata, "remote_openocd")

        assert metadata.read_text() == before
        assert list(tmp_path.iterdir()) == [metadata]
