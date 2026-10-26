#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Record whether the selected configuration file's observable state changed.

The state is only the outcome of reading the selected pathname: a digest of the
bytes read or the read error. It does not interpret the configuration; CMake
configuration remains the authority for the default runner and its diagnostics.

Configuration also writes the selected pathname to a separate selection record.
The state record may be a generated build output that cleaning removes, so the
pathname must remain available to recreate it.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def observe(config_path: str) -> str:
    try:
        with open(config_path, "rb") as stream:
            return "sha256:" + hashlib.sha256(stream.read()).hexdigest()
    except OSError as error:
        return f"error:{error.errno}"


def _replace(file: Path, content: str) -> None:
    # A partial write must not leave a record that matches nothing.
    temporary = file.with_name(file.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, file)


def record(state_file: Path, config_path: str) -> bool:
    """Rewrite the state file only when the observed state differs."""
    content = json.dumps({"path": config_path, "state": observe(config_path)})
    try:
        if state_file.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    # Unchanged state must keep its timestamp.
    _replace(state_file, content)
    return True


def record_selection(selection_file: Path, state_file: Path, config_path: str) -> None:
    """Record the selected pathname and its observed state during configuration."""
    _replace(selection_file, json.dumps(config_path))
    record(state_file, config_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection_file", type=Path)
    parser.add_argument("state_file", type=Path)
    parser.add_argument("--fail-on-change", action="store_true")
    args = parser.parse_args()
    config_path = json.loads(args.selection_file.read_text(encoding="utf-8"))
    if record(args.state_file, config_path) and args.fail_on_change:
        print(
            f"Zephyr Remote OpenOCD configuration {config_path} changed after CMake "
            "last configured this build, so its stored default runner may be stale.\n"
            "Run the build again to reconfigure it before flashing or debugging.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
