# SPDX-License-Identifier: Apache-2.0

"""Fixture-gated direct-semihosting console acceptance tests."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess

import pytest
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.process_support import assert_semihosting_acceptance
from tests.support import ROOT

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]

SESSION_PATTERN = re.compile(r"Remote OpenOCD session (\S+) workspace=(\S+) bindto=(\S+)")


class TestRealSemihosting:
    """Validate direct semihosting through the normal OpenOCD output relay."""

    @staticmethod
    def _environment(fixture):
        environment = os.environ.copy()
        environment.pop("ZRO_RECORD", None)
        environment.update(
            {
                "EXTRA_ZEPHYR_MODULES": str(ROOT),
                "ZEPHYR_REMOTE_OPENOCD_CONFIG": str(fixture["config_path"]),
            }
        )
        environment.update(
            {str(key): str(value) for key, value in fixture.get("environment", {}).items()}
        )
        return environment

    @staticmethod
    def _west(fixture, command, *, gdb_init=()):
        west = fixture.get("west") or shutil.which("west")
        if not west:
            raise AssertionError("fixture has no west executable and west is not on PATH")
        args = [
            str(west),
            command,
            "-d",
            str(fixture["build_dir"]),
            "-r",
            "remote_openocd",
            "--no-rebuild",
        ]
        runner_args = [str(item) for item in fixture.get("runner_args", ())]
        if command == "debug":
            runner_args.extend(f"--cmd-pre-init={item}" for item in fixture["semihosting_commands"])
            runner_args.extend(f"--gdb-init={item}" for item in gdb_init)
        if runner_args:
            args.extend(("--", *runner_args))
        return args

    def _assert_cleanup(self, fixture, output):
        session = SESSION_PATTERN.search(output)
        assert session is not None, output
        result = SshCommand(tuple(fixture["ssh_command"])).run(
            fixture["host"], f"test ! -e {shlex.quote(session.group(2))}", timeout=20
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    def _flash(self, fixture):
        result = subprocess.run(
            self._west(fixture, "flash"),
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=240,
        )
        assert result.returncode == 0, result.stdout
        self._assert_cleanup(fixture, result.stdout)

    def test_direct_semihosting_console_normal_completion(self, semihosting_fixture):
        fixture = semihosting_fixture
        self._flash(fixture)
        command = self._west(fixture, "debug", gdb_init=fixture["semihosting_gdb_init"])
        process = subprocess.Popen(
            command,
            cwd=fixture.get("workspace"),
            env=self._environment(fixture),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            # Forced termination is emergency cleanup, never successful completion.
            output, _ = process.communicate(timeout=float(fixture.get("timeout", 30)))
            text = output.decode("utf-8", "replace")
            assert_semihosting_acceptance(process.returncode, text, fixture["expected_output"])
            self._assert_cleanup(fixture, text)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
