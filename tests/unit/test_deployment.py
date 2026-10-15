# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO, override

import pytest
from zephyr_remote_openocd.controller import _deployment as deploy_module
from zephyr_remote_openocd.controller._deployment import build_helper_bundle
from zephyr_remote_openocd.controller._ssh import SshCommand, SshForwardSpec
from zephyr_remote_openocd.protocol.wire import (
    encode_frame,
)

from tests.process_support import managed_process, read_line


def test_missing_packaged_helper_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy_module, "files", lambda _package: tmp_path)
    with pytest.raises(deploy_module.DeploymentError, match="packaged remote helper"):
        build_helper_bundle()


class DeploymentReply(SshCommand):
    reply: tuple[int, bytes, bytes]

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        super().__init__()
        object.__setattr__(self, "reply", (returncode, stdout, stderr))

    @override
    def run(
        self,
        host: str,
        remote_command: str,
        *,
        input_data: bytes | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(remote_command, *self.reply)

    @override
    def popen(
        self,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired=None,
    ) -> Any:
        raise AssertionError("popen() is not expected in this test")

    @override
    def run_stream(
        self,
        host: str,
        remote_command: str,
        stream: BinaryIO,
        *,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("run_stream() is not expected in this test")


def test_deployment_wraps_invalid_utf8_response():
    ssh = DeploymentReply(0, b"\xff", b"")

    with pytest.raises(deploy_module.DeploymentError, match="invalid deployment response"):
        deploy_module.deploy_helper(ssh, "host", source=b"helper source")


def test_deployment_rejects_response_without_lf():
    source = b"helper source"
    response = encode_frame(
        "DEPLOYED",
        status="deployed",
        path="/home/test/helper.py",
        sha256=hashlib.sha256(source).hexdigest(),
    ).rstrip(b"\n")

    with pytest.raises(deploy_module.DeploymentError):
        deploy_module.deploy_helper(DeploymentReply(0, response, b""), "host", source=source)


def test_deployment_reports_nonzero_ssh_status_and_diagnostic():
    ssh_exit_status = 23
    ssh = DeploymentReply(ssh_exit_status, b"", b"permission denied")

    with pytest.raises(deploy_module.DeploymentError) as error:
        deploy_module.deploy_helper(ssh, "host", source=b"helper source")

    assert str(ssh_exit_status) in str(error.value)
    assert "permission denied" in str(error.value)


def test_deployment_rejects_digest_that_differs_from_source():
    source = b"helper source"
    different_source_digest = hashlib.sha256(b"different helper source").hexdigest()
    response = encode_frame(
        "DEPLOYED",
        status="deployed",
        path="/home/test/helper.py",
        sha256=different_source_digest,
    )
    ssh = DeploymentReply(0, response, b"")

    with pytest.raises(deploy_module.DeploymentError, match="invalid deployment response"):
        deploy_module.deploy_helper(ssh, "host", source=source)


def _run_bootstrap(home: Path, source: bytes) -> dict[str, object]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, "-c", deploy_module.BOOTSTRAP],
        input=source,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return json.loads(result.stdout)


def test_helper_deployment_is_content_addressed_and_prunes_stale_revisions(tmp_path):
    first_source = b"first helper revision"
    second_source = b"second helper revision"
    first = _run_bootstrap(tmp_path, first_source)
    first_path_value = first["path"]
    assert isinstance(first_path_value, str)
    first_path = Path(first_path_value)
    helper_directory = first_path.parent

    assert first["status"] == "deployed"
    assert first_path.name == f"helper-{hashlib.sha256(first_source).hexdigest()}.py"
    assert first_path.read_bytes() == first_source
    assert first_path.stat().st_mode & 0o777 == 0o600
    assert helper_directory.stat().st_mode & 0o777 == 0o700
    assert (helper_directory / ".deploy.lock").stat().st_mode & 0o777 == 0o600
    assert not list(helper_directory.glob(".helper_*"))

    reused = _run_bootstrap(tmp_path, first_source)
    assert reused["status"] == "reused"
    assert reused["path"] == str(first_path)

    stale_path = helper_directory / ("helper-" + "f" * 64 + ".py")
    stale_path.write_bytes(b"stale helper revision")
    stale_time = time.time() - 25 * 60 * 60
    os.utime(stale_path, (stale_time, stale_time))

    second = _run_bootstrap(tmp_path, second_source)
    second_path_value = second["path"]
    assert isinstance(second_path_value, str)
    second_path = Path(second_path_value)
    assert second["status"] == "deployed"
    assert second_path != first_path
    assert first_path.exists()
    assert second_path.read_bytes() == second_source
    assert not stale_path.exists()
    assert not list(helper_directory.glob(".helper_*"))


def test_helper_deployment_serializes_refresh_and_pruning(tmp_path):
    source = b"concurrent helper revision"
    environment = {**os.environ, "HOME": str(tmp_path)}
    wrapper = f"""
import fcntl,os,sys
role,events,release=sys.argv[1:]
events=int(events);release=int(release)
original_flock=fcntl.flock
original_utime=os.utime

def acquire(lock,operation):
    if role=="second":
        try:
            original_flock(lock,operation|fcntl.LOCK_NB)
        except BlockingIOError:
            os.write(events,b"blocked\\n")
            original_flock(lock,operation)
        else:
            os.write(events,b"unexpected-entry\\n")
    else:
        original_flock(lock,operation)

def refresh(path,*args,**kwargs):
    os.write(events,b"refresh\\n")
    if role=="first":
        assert os.read(release,1)==b"x"
    return original_utime(path,*args,**kwargs)

fcntl.flock=acquire
os.utime=refresh
exec({deploy_module.BOOTSTRAP!r}, {{"__name__":"__main__"}})
"""
    with ExitStack() as resources:

        def pipe():
            reader, writer = os.pipe()
            return (
                resources.enter_context(os.fdopen(reader, "rb", buffering=0)),
                resources.enter_context(os.fdopen(writer, "wb", buffering=0)),
            )

        release_reader, release_writer = pipe()
        first_events, first_event_writer = pipe()
        second_events, second_event_writer = pipe()

        def deploy(role, event_writer):
            owner = resources.enter_context(
                managed_process(
                    [
                        sys.executable,
                        "-c",
                        wrapper,
                        role,
                        str(event_writer.fileno()),
                        str(release_reader.fileno()),
                    ],
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=(event_writer.fileno(), release_reader.fileno()),
                )
            )
            process = owner.process
            assert process.stdin is not None
            process.stdin.write(source)
            process.stdin.close()
            event_writer.close()
            return process

        first = deploy("first", first_event_writer)
        assert read_line(first_events) == b"refresh\n"
        second = deploy("second", second_event_writer)
        try:
            # The first bootstrap is inside real target refresh while still
            # holding its lock. The second probes the same requested lock mode;
            # a shared-lock mutation would enter immediately and report it.
            assert read_line(second_events) == b"blocked\n"
        finally:
            release_writer.write(b"x")
        assert first.wait(timeout=30) == 0
        assert read_line(second_events) == b"refresh\n"
        assert second.wait(timeout=30) == 0
        for process, status in ((first, "deployed"), (second, "reused")):
            assert process.stdout is not None
            response = json.loads(process.stdout.read())
            assert response["status"] == status
            assert Path(response["path"]).read_bytes() == source
