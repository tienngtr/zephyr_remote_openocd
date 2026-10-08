# SPDX-License-Identifier: Apache-2.0

"""Fixture-gated direct-semihosting console acceptance tests."""

from __future__ import annotations

import os
import signal
import subprocess

import pytest

from tests.hardware_support import SemihostingFixture, hardware_operation_environment
from tests.process_support import assert_semihosting_acceptance

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]


class TestRealSemihosting:
    """Validate direct semihosting through the normal OpenOCD output relay."""

    @staticmethod
    def _west(
        fixture: SemihostingFixture, command: str, *, gdb_init: tuple[str, ...] = ()
    ) -> list[str]:
        target = fixture.target
        args = [
            str(target.build_environment.west),
            command,
            "-d",
            str(target.build_dir),
            "-r",
            "remote_openocd",
            "--no-rebuild",
        ]
        runner_args = list(target.runner_args)
        if command == "debug":
            runner_args.extend(f"--cmd-pre-init={item}" for item in fixture.operation.commands)
            runner_args.extend(f"--gdb-init={item}" for item in gdb_init)
        if runner_args:
            args.extend(("--", *runner_args))
        return args

    def test_direct_semihosting_console_normal_completion(
        self, semihosting_fixture: SemihostingFixture
    ) -> None:
        fixture = semihosting_fixture
        command = self._west(fixture, "debug", gdb_init=fixture.operation.gdb_commands)
        process = subprocess.Popen(
            command,
            cwd=fixture.target.workspace,
            env=hardware_operation_environment(fixture.target),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=fixture.operation.timeout)
            text = output.decode("utf-8", "replace")
            assert_semihosting_acceptance(process.returncode, text, fixture.operation.output)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
