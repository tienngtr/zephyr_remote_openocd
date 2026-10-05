# SPDX-License-Identifier: Apache-2.0

"""Independent sessions must progress through the real helper without hardware."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from zephyr_remote_openocd.remote import RemoteSession
from zephyr_remote_openocd.remote.flash import FlashInputs, build_flash_plan
from zephyr_remote_openocd.remote.model import RemoteSessionRequest
from zephyr_remote_openocd.remote.paths import PathPlanner
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.concurrency_support import ConcurrentSessions

pytestmark = pytest.mark.local


def test_same_probe_session_finishes_while_another_session_is_active(tmp_path: Path) -> None:
    concurrent = ConcurrentSessions(tmp_path)
    ssh = SshCommand(concurrent.ssh_argv)
    opened = (Event(), Event())

    def operation(channel: int) -> None:
        plan = build_flash_plan(
            FlashInputs(
                executable=concurrent.child_argv,
                image_type="hex",
                file=str(concurrent.image),
                elf_file=None,
                hex_file=None,
                bin_file=None,
                search_paths=(),
                config_files=(),
                load_command="program",
                verify_command="verify_image",
                serial="test-probe",
                pre_init=(f"test_channel {channel}",),
            ),
            PathPlanner(()),
        )
        session = RemoteSession.open(
            RemoteSessionRequest(
                "test-host",
                ssh,
                replace(plan.process, required_output_sentinels=("ZRO_TEST_READY",)),
                staged_files=plan.staged_files,
            )
        )
        try:
            assert session.check_openocd_exit() is None
            opened[channel].set()
            assert session.wait_for_openocd_exit(30) == 0
        finally:
            session.close()

    concurrent.check_progress(operation, opened=opened)
