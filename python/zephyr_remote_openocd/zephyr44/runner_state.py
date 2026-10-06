# SPDX-License-Identifier: Apache-2.0

"""Refresh the generated Zephyr 4.4 runner defaults without rebuilding firmware."""

from pathlib import Path
from stat import S_IMODE
from tempfile import NamedTemporaryFile

import yaml


def update_runner_default(runners_yaml: Path, default_runner: str) -> None:
    """Preserve other runner metadata and replace changed defaults atomically."""
    state = yaml.safe_load(runners_yaml.read_text())
    if state.get("flash-runner") == state.get("debug-runner") == default_runner:
        return
    state["flash-runner"] = default_runner
    state["debug-runner"] = default_runner
    mode = S_IMODE(runners_yaml.stat().st_mode)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", dir=runners_yaml.parent, delete=False) as stream:
            temporary = Path(stream.name)
            yaml.safe_dump(state, stream, sort_keys=False)
        temporary.chmod(mode)
        temporary.replace(runners_yaml)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
