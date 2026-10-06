# SPDX-License-Identifier: Apache-2.0

"""Best-effort preferred address cache; remote helper leases remain authoritative."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from .services import validate_preferred_address
from .ssh import SshCommand


def _cache_directory() -> Path:
    return Path.home() / ".cache" / "zephyr_remote_openocd" / "preferred-addresses"


def _cache_path(host: str, command: SshCommand) -> Path:
    identity = json.dumps([host, command.argv_prefix], separators=(",", ":")).encode("utf-8")
    return _cache_directory() / hashlib.sha256(identity).hexdigest()


def load_preferred_address(host: str, command: SshCommand) -> str | None:
    """Read a preferred address without making cache failure operation-fatal."""
    try:
        address = _cache_path(host, command).read_text(encoding="ascii").strip()
        validate_preferred_address(address)
        return address
    except (OSError, ValueError, RuntimeError):
        return None


def remember_preferred_address(host: str, command: SshCommand, address: str) -> None:
    """Atomically save the preferred address after required forwarding succeeds."""
    temporary: Path | None = None
    try:
        validate_preferred_address(address)
        target = _cache_path(host, command)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((address + "\n").encode("ascii"))
        os.replace(temporary, target)
    except (OSError, ValueError, RuntimeError):
        pass
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
