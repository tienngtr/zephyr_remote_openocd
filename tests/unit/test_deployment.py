# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from io import FileIO
from pathlib import Path
from typing import BinaryIO, override

import pytest
from zephyr_remote_openocd.controller import _deployment as deploy_module
from zephyr_remote_openocd.controller._deployment import build_helper_bundle
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    ProcessAcquiredHandler,
    SshCommand,
    SshForwardSpec,
)
from zephyr_remote_openocd.protocol.wire import (
    encode_frame,
)

from tests.process_support import managed_process, read_line
from tests.support import ROOT

FAULTS = ROOT / "tests/fixtures/deployment_faults.py"


def test_missing_packaged_helper_is_actionable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(deploy_module, "files", lambda _package: tmp_path)
    with pytest.raises(deploy_module.DeploymentError, match="packaged remote helper"):
        build_helper_bundle()


class DeploymentReply(SshCommand):
    reply: tuple[int, bytes, bytes]

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
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
        on_acquired: ProcessAcquiredHandler | None = None,
    ) -> ManagedSshProcess:
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


def test_deployment_wraps_invalid_utf8_response() -> None:
    ssh = DeploymentReply(0, b"\xff", b"")

    with pytest.raises(deploy_module.DeploymentError, match="invalid deployment response"):
        deploy_module.deploy_helper(ssh, "host", source=b"helper source")


def test_deployment_rejects_response_without_lf() -> None:
    source = b"helper source"
    response = encode_frame(
        "DEPLOYED",
        status="deployed",
        path="/home/test/helper.py",
        sha256=hashlib.sha256(source).hexdigest(),
    ).rstrip(b"\n")

    with pytest.raises(deploy_module.DeploymentError):
        deploy_module.deploy_helper(DeploymentReply(0, response, b""), "host", source=source)


def test_deployment_reports_nonzero_ssh_status_and_diagnostic() -> None:
    ssh_exit_status = 23
    ssh = DeploymentReply(ssh_exit_status, b"", b"permission denied")

    with pytest.raises(deploy_module.DeploymentError) as error:
        deploy_module.deploy_helper(ssh, "host", source=b"helper source")

    assert str(ssh_exit_status) in str(error.value)
    assert "permission denied" in str(error.value)


def test_deployment_rejects_digest_that_differs_from_source() -> None:
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


def _bootstrap(
    home: Path,
    source: bytes,
    fault: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    command = (
        [sys.executable, str(FAULTS), *fault]
        if fault
        else [sys.executable, "-c", deploy_module.bootstrap_program()]
    )
    return subprocess.run(
        command,
        input=source,
        capture_output=True,
        check=False,
        env=environment,
    )


def _run_bootstrap(
    home: Path,
    source: bytes,
    fault: tuple[str, ...] = (),
) -> dict[str, object]:
    result = _bootstrap(home, source, fault)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return json.loads(result.stdout)


def test_helper_deployment_is_content_addressed_and_prunes_stale_revisions(tmp_path: Path) -> None:
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


def test_corrupted_revision_at_digest_path_is_replaced(tmp_path: Path) -> None:
    source = b"intact helper revision"
    target = Path(str(_run_bootstrap(tmp_path, source)["path"]))
    assert target.name == f"helper-{hashlib.sha256(source).hexdigest()}.py"
    target.write_bytes(source[:-1])
    target.chmod(0o644)

    repaired = _run_bootstrap(tmp_path, source)

    assert repaired["status"] == "deployed"
    assert repaired["path"] == str(target)
    assert target.read_bytes() == source
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(target.parent.glob(".helper_*"))


def test_failed_install_publishes_no_revision_or_response(tmp_path: Path) -> None:
    source = b"undurable helper revision"
    # Durability fails after the complete source is written to the temporary
    # file, so neither a partial nor an unsynchronized revision may appear.
    result = _bootstrap(tmp_path, source, ("install-failure",))

    helper_directory = tmp_path / ".local/libexec/zephyr_remote_openocd/protocol_v1"
    assert result.returncode != 0
    assert result.stdout == b""
    assert not (helper_directory / f"helper-{hashlib.sha256(source).hexdigest()}.py").exists()
    assert not list(helper_directory.glob(".helper_*"))


@pytest.mark.parametrize("operation", ("stat", "unlink"))
def test_stale_revision_failure_does_not_block_deployment_or_other_pruning(
    tmp_path: Path, operation: str
) -> None:
    source = b"selected helper revision"
    helper_directory = tmp_path / ".local/libexec/zephyr_remote_openocd/protocol_v1"
    helper_directory.mkdir(parents=True)
    stale_revisions = {helper_directory / ("helper-" + digit * 64 + ".py") for digit in ("a", "b")}
    stale_time = time.time() - 25 * 60 * 60
    for stale in stale_revisions:
        stale.write_bytes(b"stale helper revision")
        os.utime(stale, (stale_time, stale_time))
    # Fail the first stale revision the bootstrap inspects or removes, whatever
    # the directory order; the real bootstrap and its filesystem effects remain.
    deployed = _run_bootstrap(
        tmp_path, source, ("stale-failure", operation, *(stale.name for stale in stale_revisions))
    )

    selected = helper_directory / f"helper-{hashlib.sha256(source).hexdigest()}.py"
    assert deployed["status"] == "deployed"
    assert deployed["path"] == str(selected.resolve())
    assert selected.read_bytes() == source
    # The failed revision is retained; the other eligible revision is still pruned.
    assert sum(stale.exists() for stale in stale_revisions) == 1
    assert not list(helper_directory.glob(".helper_*"))
    assert _run_bootstrap(tmp_path, source)["status"] == "reused"


def test_helper_deployment_serializes_refresh_and_pruning(tmp_path: Path) -> None:
    source = b"concurrent helper revision"
    environment = {**os.environ, "HOME": str(tmp_path)}
    with ExitStack() as resources:

        def pipe() -> tuple[FileIO, FileIO]:
            reader, writer = os.pipe()
            return (
                resources.enter_context(os.fdopen(reader, "rb", buffering=0)),
                resources.enter_context(os.fdopen(writer, "wb", buffering=0)),
            )

        release_reader, release_writer = pipe()
        first_events, first_event_writer = pipe()
        second_events, second_event_writer = pipe()

        def deploy(role: str, event_writer: FileIO) -> subprocess.Popen[bytes]:
            owner = resources.enter_context(
                managed_process(
                    [
                        sys.executable,
                        str(FAULTS),
                        "serialized",
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
