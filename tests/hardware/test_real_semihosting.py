# SPDX-License-Identifier: Apache-2.0

"""Fixture-gated direct-semihosting console acceptance tests."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess

import pytest
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.hardware_support import SemihostingFixture
from tests.process_support import assert_semihosting_acceptance
from tests.support import ROOT

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]
SESSION_PATTERN = re.compile(r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)")


class TestRealSemihosting:
    """Validate direct semihosting through the normal OpenOCD output relay."""

    @staticmethod
    def _environment(fixture: SemihostingFixture) -> dict[str, str]:
        target = fixture.target
        environment = os.environ.copy()
        environment.pop("ZRO_RECORD", None)
        environment.update(
            EXTRA_ZEPHYR_MODULES=str(ROOT),
            ZEPHYR_REMOTE_OPENOCD_CONFIG=str(target.config_path),
        )
        environment.update(dict(target.environment))
        return environment

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

    @staticmethod
    def _assert_cleanup(fixture: SemihostingFixture, output: str) -> None:
        session = SESSION_PATTERN.search(output)
        assert session is not None, output
        result = SshCommand(fixture.target.host.ssh_command).run(
            fixture.target.host.ssh_host,
            f"test ! -e {shlex.quote(session.group(2))}",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    def _flash(self, fixture: SemihostingFixture) -> None:
        result = subprocess.run(
            self._west(fixture, "flash"),
            cwd=fixture.target.workspace,
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=240,
        )
        assert result.returncode == 0, result.stdout
        self._assert_cleanup(fixture, result.stdout)

    def test_direct_semihosting_console_normal_completion(
        self, semihosting_fixture: SemihostingFixture
    ) -> None:
        fixture = semihosting_fixture
        self._flash(fixture)
        command = self._west(fixture, "debug", gdb_init=fixture.operation.gdb_commands)
        process = subprocess.Popen(
            command,
            cwd=fixture.target.workspace,
            env=self._environment(fixture),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=fixture.operation.timeout)
            text = output.decode("utf-8", "replace")
            assert_semihosting_acceptance(process.returncode, text, fixture.operation.output)
            self._assert_cleanup(fixture, text)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
