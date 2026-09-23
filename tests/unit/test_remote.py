# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import fcntl
import hashlib
import io
import ipaddress
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, override

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.remote import deploy as deploy_module
from zephyr_remote_openocd.remote import flash as flash_module
from zephyr_remote_openocd.remote.debug import (
    DebugInputs,
    DebugPlanError,
    build_debug_plan,
    parse_openocd_version,
    thread_info_enabled,
)
from zephyr_remote_openocd.remote.deploy import _helper_source
from zephyr_remote_openocd.remote.flash import (
    FlashInputs,
    build_flash_plan,
)
from zephyr_remote_openocd.remote.model import (
    RemotePathCheck,
    RemoteProcess,
    Service,
    StagedDirectory,
    StagedFile,
)
from zephyr_remote_openocd.remote.paths import ADDRESS_TOKEN, PathPlanner, PathPlanningError
from zephyr_remote_openocd.remote.protocol import (
    EventOrder,
    ProtocolError,
    decode_message,
    encode_message,
    validate_deployment_response,
    validate_helper_event,
    validate_openocd_version_response,
    validate_staged_response,
    write_start,
    write_stop,
)
from zephyr_remote_openocd.remote.services import (
    LOOPBACK_RANGE,
    allocate_loopback,
    random_loopback_address,
)
from zephyr_remote_openocd.remote.ssh import SshCommand
from zephyr_remote_openocd.remote.staging import StagingError, build_archive

from tests.process_support import read_line

TEST_PROCESS = RemoteProcess(("test-process",))


class TestProtocol:
    def test_round_trip_and_rejections(self):
        assert decode_message(encode_message("HELLO", value=3))["value"] == 3
        for invalid in (
            b"not-json\n",
            b"[]\n",
            b'{"version":2,"type":"HELLO"}\n',
            b'{"version":1.0,"type":"HELLO"}\n',
            b'{"version":true,"type":"HELLO"}\n',
        ):
            with pytest.raises(ProtocolError):
                decode_message(invalid)
        with pytest.raises(ProtocolError):
            encode_message("HELLO", version=2)

    def test_event_order_is_enforced(self):
        order = EventOrder()
        with pytest.raises(ProtocolError):
            order.accept(
                decode_message(
                    encode_message("PROCESS_READY", remote_address="127.64.1.1", child_pid=1)
                )
            )
        order.accept(
            decode_message(
                encode_message(
                    "SESSION_CREATED",
                    helper="helper",
                    session_id="session",
                    remote_workspace="/workspace",
                )
            )
        )
        order.accept(
            decode_message(
                encode_message(
                    "CHILD_OUTPUT",
                    stream="stdout",
                    payload="before-ready",
                    line_end=False,
                )
            )
        )
        order.accept(
            decode_message(
                encode_message("PROCESS_READY", remote_address="127.64.1.1", child_pid=1)
            )
        )
        order.accept(
            decode_message(encode_message("SESSION_CLOSED", reason="process_exit", returncode=0))
        )
        with pytest.raises(ProtocolError):
            order.accept(
                decode_message(
                    encode_message(
                        "CHILD_OUTPUT",
                        stream="stdout",
                        payload="late",
                        line_end=False,
                    )
                )
            )

    def test_start_serializers_use_validated_domain_models(self):
        stream = io.BytesIO()
        process = RemoteProcess(
            ("openocd", "--fixed", ""),
            environment=(("ZRO_TEST", "value"),),
            readiness_marker="READY",
            literal_prefix=2,
        )
        write_start(stream, process, (Service("gdb", 3333, 3333),))
        write_stop(stream)
        frames = [decode_message(line) for line in stream.getvalue().splitlines()]
        assert frames[0]["type"] == "START"
        assert frames[0]["argv"][-1] == ""
        assert frames[1] == {"version": 1, "type": "STOP"}

    def test_helper_event_unknown_fields_are_rejected(self):
        with pytest.raises(ProtocolError):
            validate_helper_event(
                decode_message(
                    encode_message(
                        "SESSION_CREATED",
                        helper="helper",
                        session_id="id",
                        remote_workspace="/work",
                        future=True,
                    )
                )
            )

    @pytest.mark.parametrize(
        ("validator", "valid", "mistyped_field"),
        (
            pytest.param(
                validate_staged_response,
                {
                    "version": 1,
                    "type": "STAGED",
                    "byte_count": 0,
                    "sha256": "0" * 64,
                    "files": [],
                    "directories": [],
                },
                {"byte_count": "0"},
                id="staged",
            ),
            pytest.param(
                validate_openocd_version_response,
                {"version": 1, "type": "OPENOCD_VERSION", "output": "OpenOCD 0.12.0"},
                {"output": None},
                id="openocd-version",
            ),
            pytest.param(
                validate_deployment_response,
                {
                    "version": 1,
                    "type": "DEPLOYED",
                    "status": "deployed",
                    "path": "/tmp/helper.py",
                    "sha256": "0" * 64,
                },
                {"sha256": 0},
                id="deployment",
            ),
        ),
    )
    def test_one_shot_response_validators_require_exact_typed_fields(
        self, validator, valid, mistyped_field
    ):
        validator(valid)

        missing_type = dict(valid)
        missing_type.pop("type")
        malformed = (
            missing_type,
            {**valid, "unexpected": True},
            {**valid, **mistyped_field},
        )
        for response in malformed:
            with pytest.raises(ProtocolError):
                validator(response)

    def test_session_closed_reason_requires_matching_returncode(self):
        validate_helper_event(
            decode_message(encode_message("SESSION_CLOSED", reason="process_exit", returncode=0))
        )
        validate_helper_event(
            decode_message(encode_message("SESSION_CLOSED", reason="requested", returncode=None))
        )
        with pytest.raises(ProtocolError):
            validate_helper_event(
                decode_message(encode_message("SESSION_CLOSED", reason="requested", returncode=0))
            )

    @pytest.mark.parametrize("payload", ("line\nbreak", "line\n"))
    def test_child_output_payload_rejects_embedded_line_delimiters(self, payload):
        with pytest.raises(ProtocolError, match="invalid required fields"):
            validate_helper_event(
                decode_message(
                    encode_message(
                        "CHILD_OUTPUT",
                        stream="stdout",
                        payload=payload,
                        line_end=False,
                    )
                )
            )

    def test_child_output_requires_boundary_metadata(self):
        valid = encode_message(
            "CHILD_OUTPUT",
            stream="stdout",
            payload="fragment",
            line_end=False,
        )
        validate_helper_event(decode_message(valid))
        for fields in (
            {},
            {"line_end": 1},
            {"line_end": False, "obsolete": False},
            {"line_end": False, "unexpected": True},
        ):
            with pytest.raises(ProtocolError):
                validate_helper_event(
                    decode_message(
                        encode_message(
                            "CHILD_OUTPUT",
                            stream="stdout",
                            payload="fragment",
                            **fields,
                        )
                    )
                )

    def test_child_output_boundary_states_are_unambiguous(self):
        for payload, line_end in (("", False),):
            with pytest.raises(ProtocolError):
                validate_helper_event(
                    decode_message(
                        encode_message(
                            "CHILD_OUTPUT",
                            stream="stdout",
                            payload=payload,
                            line_end=line_end,
                        )
                    )
                )

    def test_event_order_accepts_output_until_terminal_event(self):
        order = EventOrder()
        order.accept(
            decode_message(
                encode_message(
                    "SESSION_CREATED",
                    helper="helper",
                    session_id="session",
                    remote_workspace="/workspace",
                )
            )
        )
        order.accept(
            decode_message(
                encode_message("PROCESS_READY", remote_address="127.64.1.1", child_pid=1)
            )
        )

        def output(stream, payload, *, line_end=False):
            order.accept(
                decode_message(
                    encode_message(
                        "CHILD_OUTPUT",
                        stream=stream,
                        payload=payload,
                        line_end=line_end,
                    )
                )
            )

        output("stdout", "first")
        output("stderr", "still open")
        output("stdout", "later output")
        order.accept(
            decode_message(encode_message("SESSION_CLOSED", reason="process_exit", returncode=0))
        )
        with pytest.raises(ProtocolError):
            output("stderr", "late output")


def test_missing_packaged_remote_helper_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy_module, "files", lambda _package: tmp_path)
    with pytest.raises(deploy_module.DeploymentError, match="packaged remote helper"):
        _helper_source()


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
    def popen(self, host: str, remote_command: str, *extra_args: str) -> Any:
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
    response = encode_message(
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
    helper_directory = tmp_path / ".local/libexec/zephyr_remote_openocd/protocol_v1"
    helper_directory.mkdir(parents=True)
    lock_path = helper_directory / ".deploy.lock"
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path)
    marker_read_fd, marker_write_fd = os.pipe()
    environment["ZRO_TEST_LOCK_MARKER_FD"] = str(marker_write_fd)
    wrapper = f"""
import fcntl
import os

original_flock = fcntl.flock

def report_lock_attempt(file_object, operation):
    os.write(int(os.environ[\"ZRO_TEST_LOCK_MARKER_FD\"]), b\"before-flock\\n\")
    return original_flock(file_object, operation)

fcntl.flock = report_lock_attempt
exec({deploy_module.BOOTSTRAP!r}, {{\"__name__\": \"__main__\"}})
"""

    process = None
    try:
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(
                [sys.executable, "-c", wrapper],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                pass_fds=(marker_write_fd,),
            )
            os.close(marker_write_fd)
            assert process.stdin is not None
            process.stdin.write(b"concurrent helper revision")
            process.stdin.close()

            with os.fdopen(marker_read_fd, "rb", buffering=0) as marker:
                assert read_line(marker, timeout=5) == b"before-flock\n"
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        raise

    process.wait(timeout=5)
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = process.stdout.read()
    stderr = process.stderr.read()
    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    response = json.loads(stdout)
    assert Path(response["path"]).read_bytes() == b"concurrent helper revision"


def test_packaged_remote_helper_is_available_and_valid_python():
    source = _helper_source()
    assert source
    compile(source, "remote_helper.py", "exec")


class TestStaging:
    def test_build_archive_preserves_binary_and_empty_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "empty").write_bytes(b"")
            (root / "binary").write_bytes(bytes(range(256)) + b"\0")
            archive = build_archive(
                (
                    StagedFile(root / "empty", PurePosixPath("a/empty")),
                    StagedFile(root / "binary", PurePosixPath("b/binary")),
                ),
                spool_limit=1,
            )
            assert archive.byte_count == 257
            assert archive.sha256 == hashlib.sha256(bytes(range(256)) + b"\0").hexdigest()
            assert archive.directories == ()
            with tarfile.open(fileobj=archive.stream, mode="r:*") as packaged:
                assert packaged.getnames() == ["a/empty", "b/binary"]
                empty = packaged.extractfile("a/empty")
                binary = packaged.extractfile("b/binary")
                assert empty is not None and binary is not None
                assert empty.read() == b""
                assert binary.read() == bytes(range(256)) + b"\0"
            archive.stream.close()

    def test_build_archive_preserves_path_components_with_spaces(self, tmp_path: Path):
        source = tmp_path / "source file.bin"
        source.write_bytes(b"payload")
        archive = build_archive(
            (StagedFile(source, PurePosixPath("directory with spaces/file name.bin")),)
        )
        with tarfile.open(fileobj=archive.stream, mode="r:*") as packaged:
            assert packaged.getnames() == ["directory with spaces/file name.bin"]
            content = packaged.extractfile(packaged.getmember(packaged.getnames()[0]))
            assert content is not None
            assert content.read() == b"payload"
        archive.stream.close()

    def test_build_archive_preserves_empty_directories_and_file_digest(self, tmp_path: Path):
        root = tmp_path / "search"
        (root / "empty").mkdir(parents=True)
        (root / "nested" / "also-empty").mkdir(parents=True)
        payload = root / "nested" / "payload.bin"
        payload.write_bytes(b"payload")
        archive = build_archive(
            (
                StagedDirectory(root, PurePosixPath("trees/search_0")),
                StagedDirectory(root / "empty", PurePosixPath("trees/search_0/empty")),
                StagedDirectory(root / "nested", PurePosixPath("trees/search_0/nested")),
                StagedDirectory(
                    root / "nested" / "also-empty",
                    PurePosixPath("trees/search_0/nested/also-empty"),
                ),
                StagedFile(payload, PurePosixPath("trees/search_0/nested/payload.bin")),
            )
        )
        assert archive.files == ("trees/search_0/nested/payload.bin",)
        assert archive.directories == (
            "trees/search_0",
            "trees/search_0/empty",
            "trees/search_0/nested",
            "trees/search_0/nested/also-empty",
        )
        assert archive.byte_count == len(b"payload")
        assert archive.sha256 == hashlib.sha256(b"payload").hexdigest()
        with tarfile.open(fileobj=archive.stream, mode="r:*") as packaged:
            assert packaged.getnames() == [
                "trees/search_0",
                "trees/search_0/empty",
                "trees/search_0/nested",
                "trees/search_0/nested/also-empty",
                "trees/search_0/nested/payload.bin",
            ]
            assert packaged.getmember("trees/search_0").isdir()
        archive.stream.close()

    @pytest.mark.parametrize(
        "entries",
        (
            (
                StagedFile(Path("a"), PurePosixPath("root")),
                StagedFile(Path("b"), PurePosixPath("root/child")),
            ),
            (
                StagedDirectory(Path("a"), PurePosixPath("root")),
                StagedFile(Path("b"), PurePosixPath("root")),
            ),
        ),
        ids=("file-ancestor", "file-directory-duplicate"),
    )
    def test_build_archive_rejects_manifest_conflicts_before_reading_sources(self, entries):
        with pytest.raises(StagingError, match="(ancestor conflict|duplicate)"):
            build_archive(entries)


class TestRemoteModels:
    @pytest.mark.parametrize(
        ("changes", "message"),
        (
            ({"argv": ()}, "argv"),
            ({"argv": ("",)}, "argv"),
            ({"argv": ("openocd", 1)}, "argv"),
            ({"environment": (("NAME", "1"), ("NAME", "2"))}, "environment names"),
            ({"environment": (("BAD=NAME", "value"),)}, "environment names"),
            ({"environment": (("NAME", "bad\0value"),)}, "environment values"),
            ({"required_paths": (object(),)}, "path checks"),
            ({"readiness_marker": ""}, "readiness marker"),
            ({"readiness_marker": "not a token"}, "readiness marker"),
            ({"readiness_timeout": True}, "readiness timeout"),
            ({"readiness_timeout": float("inf")}, "readiness timeout"),
            ({"readiness_timeout": 0}, "readiness timeout"),
            ({"literal_prefix": True}, "literal argv prefix"),
            ({"literal_prefix": -1}, "literal argv prefix"),
            ({"literal_prefix": 2}, "literal argv prefix"),
        ),
    )
    def test_remote_process_rejects_invalid_domain_values(self, changes, message):
        fields = {"argv": ("openocd",), **changes}
        with pytest.raises(ValueError, match=message):
            RemoteProcess(**fields)

    @pytest.mark.parametrize(
        ("path", "kind", "message"),
        (
            ("", "file", "non-empty path"),
            ("bad\0path", "file", "non-empty path"),
            ("path", "socket", "kind is invalid"),
        ),
    )
    def test_remote_path_check_rejects_invalid_values(self, path, kind, message):
        with pytest.raises(ValueError, match=message):
            RemotePathCheck(path, kind)

    @pytest.mark.parametrize(
        ("arguments", "message"),
        (
            (("", 1234, 3333), "name"),
            ((1, 1234, 3333), "name"),
            ((True, 1234, 3333), "name"),
            (("gdb", True, 3333), "local port"),
            (("gdb", 0, 3333), "local port"),
            (("gdb", 1234, 65536), "remote port"),
        ),
    )
    def test_service_rejects_invalid_values(self, arguments, message):
        with pytest.raises(ValueError, match=message):
            Service(*arguments)


class TestAllocation:
    def test_range_and_exhaustion(self):
        assert ipaddress.IPv4Address(random_loopback_address()) in LOOPBACK_RANGE
        calls = []

        def collision(address):
            calls.append(address)
            raise OSError("occupied")

        with pytest.raises(RuntimeError):
            allocate_loopback(collision, attempts=3)
        assert len(calls) == 3


class TestFlashPlanning:
    @pytest.mark.parametrize(
        ("image_type", "load_command", "verify_command", "flash_address", "required"),
        (
            ("bin", None, None, None, ("load command", "flash address")),
            ("hex", None, None, None, ("load", "verify")),
        ),
    )
    def test_missing_required_flash_metadata_fails_during_planning(
        self,
        image_type: str,
        load_command: str | None,
        verify_command: str | None,
        flash_address: str | None,
        required: tuple[str, ...],
    ):
        planner = PathPlanner(())
        inputs = FlashInputs(
            executable="openocd",
            image_type=image_type,
            file=None,
            elf_file=None,
            hex_file=None,
            bin_file=None,
            search_paths=(),
            config_files=(),
            load_command=load_command,
            verify_command=verify_command,
            flash_address=flash_address,
        )

        with pytest.raises(flash_module.FlashPlanError) as error:
            build_flash_plan(inputs, planner)

        message = str(error.value).lower()
        assert all(item in message for item in required)
        assert not planner.staged_files
        assert not planner.remote_checks

    def test_plan_directory_records_empty_root_and_nested_directories(self, tmp_path: Path):
        root = tmp_path / "search"
        (root / "empty").mkdir(parents=True)
        (root / "nested" / "also-empty").mkdir(parents=True)
        planner = PathPlanner(())

        planned = planner.plan_directory(root, "search_0")

        assert planned.remote == "{workspace}/staged/trees/search_0"
        assert [str(item.destination) for item in planner.staged_files] == [
            "trees/search_0",
            "trees/search_0/empty",
            "trees/search_0/nested",
            "trees/search_0/nested/also-empty",
        ]
        assert all(isinstance(item, StagedDirectory) for item in planner.staged_files)

    def test_hex_plan_preserves_ports_and_rewrites_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            support = root / "scripts" / "board"
            support.mkdir(parents=True)
            config = support / "openocd.cfg"
            config.write_text("source [find common.cfg]\n")
            (support / "common.cfg").write_text("# common\n")
            image = root / "zephyr.hex"
            image.write_text(":00000001FF\n")
            planner = PathPlanner(())
            plan = build_flash_plan(
                FlashInputs(
                    executable="/opt/openocd",
                    image_type="hex",
                    file=None,
                    elf_file=None,
                    hex_file=str(image),
                    bin_file=None,
                    search_paths=(str(root / "scripts"),),
                    config_files=(str(config),),
                    load_command="flash write_image erase",
                    verify_command="verify_image",
                    pre_init=("gdb_port 7777", "tcl_port 8888", "telnet_port 9999"),
                    verify=True,
                ),
                planner,
                (("PROBE", "value"),),
            )
            argv = plan.process.argv
            assert f"bindto {ADDRESS_TOKEN}" in argv
            assert "gdb_port 7777" in argv
            assert "gdb_port disabled" not in argv
            assert plan.process.environment == (("PROBE", "value"),)
            assert "{workspace}/staged/trees/search_0/board/openocd.cfg" in argv
            assert len([item for item in plan.staged_files if item.source == config]) == 1
            assert plan.process.argv[-4:] == ("-c", "reset run", "-c", "shutdown")

    def test_elf_plan_resumes_before_shutdown(self, monkeypatch, tmp_path):
        image = tmp_path / "image.elf"
        image.write_bytes(b"not inspected")
        monkeypatch.setattr(flash_module, "_elf_entry", lambda _: "0x0000000008000000")
        plan = build_flash_plan(
            FlashInputs(
                executable="openocd",
                image_type="elf",
                file=None,
                elf_file=str(image),
                hex_file=None,
                bin_file=None,
                search_paths=(),
                config_files=(),
            ),
            PathPlanner(()),
        )
        assert plan.process.argv[-4:] == (
            "-c",
            "resume 0x0000000008000000",
            "-c",
            "shutdown",
        )

    def test_longest_mapping_and_remote_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specific = root / "specific"
            specific.mkdir()
            image = specific / "image.hex"
            image.write_text("image")
            planner = PathPlanner(
                (
                    PathMapping(root, PurePosixPath("/general")),
                    PathMapping(specific, PurePosixPath("/specific")),
                )
            )
            planned = planner.plan_file(image, "firmware")
            assert planned.remote == "/specific/image.hex"
            assert planner.remote_checks[0].path == "/specific/image.hex"

    def test_paths_with_spaces_remain_single_argv_and_quoted_tcl_words(self, tmp_path: Path):
        local = tmp_path / "local images"
        local.mkdir()
        image = local / "firmware image.hex"
        image.write_text(":00000001FF\n")
        config = local / "board config.cfg"
        config.write_text("# config\n")
        plan = build_flash_plan(
            FlashInputs(
                executable="/remote tools/openocd",
                image_type="hex",
                file=str(image),
                elf_file=None,
                hex_file=str(image),
                bin_file=None,
                search_paths=(str(local),),
                config_files=(str(config),),
                load_command="program",
                verify_command="verify_image",
            ),
            PathPlanner((PathMapping(local, PurePosixPath("/remote tree")),)),
        )
        argv = plan.process.argv
        assert argv[0] == "/remote tools/openocd"
        assert argv[argv.index("-s") + 1] == "/remote tree"
        assert argv[argv.index("-f") + 1] == "/remote tree/board config.cfg"
        assert 'program "/remote tree/firmware image.hex"' in argv
        assert plan.process.required_paths[-1].path == "/remote tree/firmware image.hex"

    def test_escaping_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            external = Path(outside) / "external.cfg"
            external.write_text("external")
            (root / "escape.cfg").symlink_to(external)
            with pytest.raises(PathPlanningError, match="escapes"):
                PathPlanner(()).plan_directory(root, "search_0")

    def test_bin_plan_preserves_address_erase_and_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.bin"
            image.write_bytes(b"binary")
            plan = build_flash_plan(
                FlashInputs(
                    executable="/openocd",
                    image_type="bin",
                    file=None,
                    elf_file=None,
                    hex_file=None,
                    bin_file=str(image),
                    search_paths=(),
                    config_files=(),
                    load_command="program",
                    verify_command="verify",
                    flash_address="0x8000000",
                    erase=True,
                    erase_commands=("mass_erase",),
                    verify=True,
                ),
                PathPlanner(()),
            )
            joined = "\n".join(plan.process.argv)
            assert "mass_erase" in joined
            assert "program " in joined
            assert "verify " in joined
            assert "0x8000000" in joined

    def test_serial_is_set_before_board_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "openocd.cfg"
            config.write_text("adapter driver ftdi\n")
            image = Path(directory) / "image.hex"
            image.write_text(":00000001FF\n")
            plan = build_flash_plan(
                FlashInputs(
                    executable="openocd",
                    image_type="hex",
                    file=str(image),
                    elf_file=None,
                    hex_file=str(image),
                    bin_file=None,
                    search_paths=(),
                    config_files=(str(config),),
                    load_command="program",
                    verify_command="verify_image",
                    serial="ES-FT4232H-02",
                ),
                PathPlanner(()),
            )
            argv = plan.process.argv
            assert argv.index("set _ZEPHYR_BOARD_SERIAL ES-FT4232H-02") < argv.index("-f")


class TestDebugPlanning:
    def inputs(self, root, command="debug", **changes):
        config = root / "openocd.cfg"
        config.write_text("# config\n")
        values = dict(
            command=command,
            executable="/remote/openocd",
            gdb="/local/gdb",
            elf_file="/local/zephyr.elf",
            search_paths=(str(root),),
            config_files=(str(config),),
            readiness_marker="ZRO_READY_test",
        )
        values.update(changes)
        return DebugInputs(**values)

    @pytest.mark.parametrize(
        ("missing", "message"),
        (("gdb", "GDB executable"), ("elf_file", "ELF file")),
    )
    def test_missing_client_metadata_fails_during_planning(
        self, tmp_path: Path, missing: str, message: str
    ):
        changes = {missing: None}
        planner = PathPlanner(())

        with pytest.raises(DebugPlanError) as error:
            build_debug_plan(
                self.inputs(tmp_path, search_paths=(), config_files=(), **changes),
                planner,
            )

        assert message.lower() in str(error.value).lower()
        assert not planner.staged_files
        assert not planner.remote_checks

    def test_command_semantics_and_client_ordering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            debug = build_debug_plan(
                self.inputs(
                    root,
                    gdb_init=("info registers", "quit"),
                ),
                PathPlanner(()),
            )
            assert debug.gdb_argv is not None
            services = {item.name: item for item in debug.services}
            assert services == {
                "gdb": Service("gdb", 3333, 3333),
                "tcl": Service("tcl", 6333, 6333),
                "telnet": Service("telnet", 4444, 4444),
            }
            assert debug.gdb_argv[-6:] == (
                "-ex",
                "load",
                "-ex",
                "info registers",
                "-ex",
                "quit",
            )
            assert "halt" in debug.process.argv
            attach = build_debug_plan(self.inputs(root, "attach"), PathPlanner(()))
            assert attach.gdb_argv is not None
            assert "load" not in attach.gdb_argv
            server = build_debug_plan(
                self.inputs(
                    root,
                    "debugserver",
                    serial="probe",
                    reset_halt="reset init",
                ),
                PathPlanner(()),
            )
            assert server.gdb_argv is None
            assert "set _ZEPHYR_BOARD_SERIAL probe" in server.process.argv
            assert server.process.argv.index(
                "set _ZEPHYR_BOARD_SERIAL probe"
            ) < server.process.argv.index("-f")
            assert "reset init" in server.process.argv

    def test_local_and_remote_paths_with_spaces_remain_argv_elements(self, tmp_path: Path):
        local = tmp_path / "debug support"
        local.mkdir()
        config = local / "board config.cfg"
        config.write_text("# config\n")
        elf = tmp_path / "build output" / "zephyr image.elf"
        elf.parent.mkdir()
        elf.write_bytes(b"elf")
        plan = build_debug_plan(
            self.inputs(
                local,
                executable="/remote tools/openocd",
                gdb="/local tools/gdb",
                elf_file=str(elf),
                config_files=(str(config),),
            ),
            PathPlanner((PathMapping(local, PurePosixPath("/remote support")),)),
        )
        assert plan.process.argv[0] == "/remote tools/openocd"
        assert plan.process.argv[plan.process.argv.index("-s") + 1] == "/remote support"
        assert plan.process.argv[plan.process.argv.index("-f") + 1] == (
            "/remote support/board config.cfg"
        )
        assert plan.gdb_argv is not None
        assert plan.gdb_argv[0] == "/local tools/gdb"
        assert str(elf) in plan.gdb_argv

    def test_disabled_services_and_distinct_gdb_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = build_debug_plan(
                self.inputs(
                    Path(directory),
                    tcl_port="disabled",
                    telnet_port="disabled",
                    gdb_port="3344",
                    gdb_client_port="3355",
                ),
                PathPlanner(()),
            )
            assert plan.gdb_argv is not None
            assert plan.services == (Service("gdb", 3355, 3344),)
            assert "target extended-remote 127.0.0.1:3355" in plan.gdb_argv
            assert "tcl_port disabled" in plan.process.argv
            with pytest.raises(DebugPlanError, match="gdb_port must be enabled"):
                build_debug_plan(self.inputs(Path(directory), gdb_port="disabled"), PathPlanner(()))

    def test_version_parsing_and_thread_info_decision(self):
        old = parse_openocd_version("Open On-Chip Debugger 0.11.0")
        development = parse_openocd_version("Open On-Chip Debugger 0.11.0+dev")
        current = parse_openocd_version("Open On-Chip Debugger 0.12.0-01050")
        assert not thread_info_enabled(True, old)
        assert not thread_info_enabled(True, development)
        assert thread_info_enabled(True, current)
        assert not thread_info_enabled(False, None)
        with pytest.raises(DebugPlanError):
            parse_openocd_version("unknown")
        with pytest.raises(DebugPlanError):
            thread_info_enabled(True, None)

    def test_rtos_command_is_conditional_and_after_pre_init(self):
        with tempfile.TemporaryDirectory() as directory:
            version = parse_openocd_version("Open On-Chip Debugger 0.12.0")
            plan = build_debug_plan(
                self.inputs(
                    Path(directory),
                    pre_init=("adapter speed 1000",),
                    thread_info_requested=True,
                    openocd_version=version,
                ),
                PathPlanner(()),
            )
            argv = plan.process.argv
            assert argv.index("adapter speed 1000") < argv.index(
                "$_TARGETNAME configure -rtos Zephyr"
            )
            assert plan.rtos_awareness

    def test_standalone_rtt_uses_batch_gdb_and_deferred_service(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = build_debug_plan(
                self.inputs(
                    Path(directory),
                    "rtt",
                    rtt_address=0x20001000,
                    rtt_port=5566,
                    gdb_init=("set pagination off",),
                ),
                PathPlanner(()),
            )
            assert plan.gdb_argv is not None
            services = {item.name: item for item in plan.services}
            assert services == {
                "gdb": Service("gdb", 3333, 3333),
                "tcl": Service("tcl", 6333, 6333),
                "telnet": Service("telnet", 4444, 4444),
            }
            assert plan.rtt_service == Service("rtt", 5566, 5566)
            assert plan.rtt_setup == "batch_gdb"
            assert plan.launches_rtt_client
            assert "--batch" in plan.gdb_argv
            assert plan.gdb_argv.index("set pagination off") < plan.gdb_argv.index(
                'monitor rtt setup 0x20001000 0x10 "SEGGER RTT"'
            )
            assert "monitor rtt server start 5566 0" in plan.gdb_argv
            assert "rtt server start 5566 0" not in plan.process.argv

    def test_rtt_server_is_ready_with_openocd_and_never_launches_client(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in ("debug", "debugserver"):
                plan = build_debug_plan(
                    self.inputs(
                        Path(directory),
                        command,
                        rtt_address=0x20002000,
                        rtt_port="5577",
                        rtt_server=True,
                    ),
                    PathPlanner(()),
                )
                services = {item.name: item for item in plan.services}
                assert services["rtt"] == Service("rtt", 5577, 5577)
                assert "rtt server start 5577 0" in plan.process.argv
                assert plan.rtt_setup == "openocd_startup"
                assert not plan.launches_rtt_client

    def test_rtt_requires_control_block_and_enabled_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with pytest.raises(DebugPlanError, match="RTT control block not found"):
                build_debug_plan(self.inputs(root, "rtt"), PathPlanner(()))
            with pytest.raises(DebugPlanError, match="rtt_port must be enabled"):
                build_debug_plan(
                    self.inputs(root, "rtt", rtt_address=0x2000, rtt_port="disabled"),
                    PathPlanner(()),
                )
            with pytest.raises(DebugPlanError, match="rtt_port conflicts"):
                build_debug_plan(
                    self.inputs(root, "rtt", rtt_address=0x2000, rtt_port=3333),
                    PathPlanner(()),
                )
