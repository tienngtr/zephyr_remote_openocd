# SPDX-License-Identifier: Apache-2.0

"""Configured remote OpenOCD version query."""

from __future__ import annotations

import subprocess

from zephyr_remote_openocd.helper import _streams as streams


def openocd_version(argv):
    if (
        not argv
        or not isinstance(argv[0], str)
        or not argv[0]
        or not all(isinstance(item, str) for item in argv[1:])
    ):
        raise ValueError("OpenOCD command must be a non-empty argv")
    result = subprocess.run(
        [*argv, "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode:
        raise RuntimeError(f"OpenOCD version query failed ({result.returncode}): {output.strip()}")
    streams.emit("OPENOCD_VERSION", output=output)
