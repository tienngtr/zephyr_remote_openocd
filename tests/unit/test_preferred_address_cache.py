# SPDX-License-Identifier: Apache-2.0

"""Preferred addresses remain disposable, private, and independent per SSH remote."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from zephyr_remote_openocd.remote import preferred_address_cache
from zephyr_remote_openocd.remote.ssh import SshCommand


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "preferred-addresses"
    monkeypatch.setattr(preferred_address_cache, "_cache_directory", lambda: root)
    return root


def test_preferred_address_persists_per_ssh_remote_and_can_be_lost(cache: Path):
    command = SshCommand(("ssh", "-F", "test-config"))
    assert preferred_address_cache.load_preferred_address("target", command) is None
    preferred_address_cache.remember_preferred_address("target", command, "127.64.0.7")
    assert preferred_address_cache.load_preferred_address("target", command) == "127.64.0.7"
    assert preferred_address_cache.load_preferred_address("other", command) is None
    assert (
        preferred_address_cache.load_preferred_address(
            "target", SshCommand(("ssh", "-F", "other-config"))
        )
        is None
    )
    files = list(cache.iterdir())
    assert len(files) == 1
    assert files[0].read_text() == "127.64.0.7\n"
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert cache.stat().st_mode & 0o777 == 0o700
    files[0].unlink()
    assert preferred_address_cache.load_preferred_address("target", command) is None


@pytest.mark.parametrize("content", (b"garbage", b"127.0.0.1", b"\xff", b"127.64.0.0"))
def test_invalid_preferred_address_cache_data_is_ignored(cache: Path, content: bytes):
    command = SshCommand()
    preferred_address_cache.remember_preferred_address("target", command, "127.64.0.7")
    next(cache.iterdir()).write_bytes(content)
    assert preferred_address_cache.load_preferred_address("target", command) is None


def test_unusable_preferred_address_cache_path_does_not_fail_operations(cache: Path):
    cache.write_text("not a directory")
    preferred_address_cache.remember_preferred_address("target", SshCommand(), "127.64.0.7")
    assert preferred_address_cache.load_preferred_address("target", SshCommand()) is None


def test_failed_atomic_update_preserves_previous_preferred_address(cache: Path, monkeypatch):
    command = SshCommand()
    preferred_address_cache.remember_preferred_address("target", command, "127.64.0.7")

    def fail_replace(_source, _destination):
        raise OSError("cache filesystem unavailable")

    monkeypatch.setattr(os, "replace", fail_replace)
    preferred_address_cache.remember_preferred_address("target", command, "127.64.0.8")
    assert preferred_address_cache.load_preferred_address("target", command) == "127.64.0.7"
    assert len(list(cache.iterdir())) == 1
