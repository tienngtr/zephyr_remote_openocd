# SPDX-License-Identifier: Apache-2.0

"""Flash prepared builds, including firmware preconditions, onto hardware."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.hardware_support import (
    FirmwarePrecondition,
    PreparedTarget,
    hardware_operation_environment,
)
from tests.process_support import run_process


def west_flash(target: PreparedTarget, build_dir: Path) -> subprocess.CompletedProcess[str]:
    """Flash one prepared build through the remote runner with the profile arguments."""
    command = [
        str(target.build_environment.west),
        "flash",
        "-d",
        str(build_dir),
        "-r",
        "remote_openocd",
        "--no-rebuild",
    ]
    if target.runner_args:
        command.extend(("--", *target.runner_args))
    return run_process(
        command,
        cwd=target.workspace,
        env=hardware_operation_environment(target),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=180,
    )


def establish_precondition(target: PreparedTarget, precondition: FirmwarePrecondition) -> None:
    """Flash ``precondition`` so the operation starts from known firmware."""
    result = west_flash(target, precondition.build_dir)
    assert result.returncode == 0, result.stdout
