# SPDX-License-Identifier: Apache-2.0

"""Source version policy is independent of builds and a Git checkout."""

from pathlib import Path

import pytest
from zephyr_remote_openocd._zephyr import UnsupportedZephyrError, adapter_module


@pytest.mark.parametrize("minor", (4, 5))
@pytest.mark.parametrize("patch", (0, 1, 2, 42))
def test_stable_release_family(tmp_path: Path, minor: int, patch: int) -> None:
    version = tmp_path / "VERSION"
    version.write_text(
        f"VERSION_MAJOR = 4\nVERSION_MINOR = {minor}\nPATCHLEVEL = {patch}\n"
        "VERSION_TWEAK = 0\nEXTRAVERSION =\n"
    )
    assert adapter_module(version).endswith(f".zephyr4{minor}.runner")


@pytest.mark.parametrize(
    "major,minor,patch,tweak,extra",
    (
        (4, 4, 99, 0, ""),
        (4, 5, 99, 0, ""),
        (4, 4, 0, 0, "rc1"),
        (4, 4, 0, 1, ""),
        (4, 6, 0, 0, ""),
        (5, 0, 0, 0, ""),
    ),
)
def test_unsupported_sources_have_no_fallback(
    tmp_path: Path, major: int, minor: int, patch: int, tweak: int, extra: str
) -> None:
    version = tmp_path / "VERSION"
    version.write_text(
        f"VERSION_MAJOR = {major}\nVERSION_MINOR = {minor}\nPATCHLEVEL = {patch}\n"
        f"VERSION_TWEAK = {tweak}\nEXTRAVERSION = {extra}\n"
    )
    with pytest.raises(UnsupportedZephyrError, match="unsupported Zephyr") as raised:
        adapter_module(version)
    assert str(version) in str(raised.value)


@pytest.mark.parametrize(
    "contents",
    (
        None,
        "",
        "VERSION_MAJOR = four",
        "VERSION_MAJOR = 4\nVERSION_MINOR = 4\nPATCHLEVEL = -1\nVERSION_TWEAK = 0\nEXTRAVERSION =",
    ),
)
def test_unreadable_or_invalid_version_reports_source(tmp_path: Path, contents: str | None) -> None:
    version = tmp_path / "VERSION"
    if contents is not None:
        version.write_text(contents)
    with pytest.raises(UnsupportedZephyrError, match="cannot read Zephyr version") as raised:
        adapter_module(version)
    assert str(version) in str(raised.value)
