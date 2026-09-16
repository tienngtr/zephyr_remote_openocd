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
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
    SessionState,
    StagedFile,
)
from zephyr_remote_openocd.remote.paths import ADDRESS_TOKEN, PathPlanner, PathPlanningError
from zephyr_remote_openocd.remote.protocol import (
    EventOrder,
    ProtocolError,
    decode_message,
    encode_message,
    validate_helper_event,
    write_start,
    write_stop,
)
from zephyr_remote_openocd.remote.services import (
    LOOPBACK_RANGE,
    allocate_loopback,
    random_loopback_address,
)
from zephyr_remote_openocd.remote.session import (
    BackendSession,
    RemoteSession,
    SessionBackend,
    SessionError,
)
from zephyr_remote_openocd.remote.ssh import SshCommand
from zephyr_remote_openocd.remote.staging import build_archive


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
            decode_message(encode_message("CHILD_OUTPUT", stream="stdout", payload="before-ready"))
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
                decode_message(encode_message("CHILD_OUTPUT", stream="stdout", payload="late"))
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


def test_missing_packaged_remote_helper_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy_module, "files", lambda _package: tmp_path)
    with pytest.raises(deploy_module.DeploymentError, match="packaged remote helper"):
        _helper_source()


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
    first_path = Path(first["path"])
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
    second_path = Path(second["path"])
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

    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen(
            [sys.executable, "-c", deploy_module.BOOTSTRAP],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            process.communicate(b"concurrent helper revision", timeout=0.5)

    stdout, stderr = process.communicate(timeout=5)
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
                    StagedFile(root / "empty", "a/empty"),
                    StagedFile(root / "binary", "b/binary"),
                ),
                spool_limit=1,
            )
            assert archive.byte_count == 257
            assert archive.sha256 == hashlib.sha256(bytes(range(256)) + b"\0").hexdigest()
            with tarfile.open(fileobj=archive.stream, mode="r:*") as packaged:
                assert packaged.getnames() == ["a/empty", "b/binary"]
                assert packaged.extractfile("a/empty").read() == b""
                assert packaged.extractfile("b/binary").read() == bytes(range(256)) + b"\0"
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


class _FakeSession(BackendSession):
    def __init__(self):
        self.actions = []
        self.returncode = None
        self.stage_error = None
        self.start_error = None
        self.forward_error = None
        self.poll_error = None
        self.wait_error = None
        self.close_error = None

    def stage(self, files):
        self.actions.append(("stage", tuple(files)))
        if self.stage_error is not None:
            raise self.stage_error

    def start(self, services):
        self.actions.append(("start", tuple(services)))
        if self.start_error is not None:
            raise self.start_error
        return SessionDescriptor(SessionAllocation("id", "/workspace"), "127.64.0.1")

    def forward(self, services):
        self.actions.append(("forward", tuple(services)))
        if self.forward_error is not None:
            raise self.forward_error

    def poll(self):
        if self.poll_error is not None:
            raise self.poll_error
        return self.returncode

    def wait(self, timeout=None):
        if self.wait_error is not None:
            raise self.wait_error
        return 9

    def close(self):
        self.actions.append(("close",))
        if self.close_error is not None:
            raise self.close_error


class _FakeBackend(SessionBackend):
    def __init__(self):
        self.session = _FakeSession()

    def create(self, request):
        return self.session


class TestSession:
    def request(self):
        return RemoteSessionRequest("host", SshCommand(), services=(Service("gdb", 1234, 3333),))

    def test_success_context_and_helper_loss(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        with session:
            assert session.state == SessionState.READY
        assert session.state == SessionState.CLOSED
        session = RemoteSession(self.request(), backend := _FakeBackend())
        session.start()
        backend.session.returncode = 7
        assert session.poll() == 7
        assert session.termination_returncode == 7
        assert session.state == SessionState.FAILED

    @pytest.mark.parametrize("failure", ("stage_error", "start_error"))
    def test_start_failure_closes_backend_and_preserves_error(self, failure):
        backend = _FakeBackend()
        error = RuntimeError(f"injected {failure}")
        setattr(backend.session, failure, error)
        session = RemoteSession(self.request(), backend)

        with pytest.raises(RuntimeError, match=failure) as raised:
            session.start()

        assert raised.value is error
        assert session.state == SessionState.FAILED
        assert backend.session.actions[-1] == ("close",)

    def test_dynamic_forward_accepts_distinct_services(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        session.start()
        rtt = Service("rtt", 5555, 5555)
        session.forward((rtt,))
        assert ("forward", (rtt,)) in backend.session.actions
        session.close()

    @pytest.mark.parametrize(
        ("addition", "message"),
        (
            (Service("gdb", 5555, 5555), "service names must remain unique"),
            (Service("rtt", 1234, 5555), "local service ports must remain unique"),
            (Service("rtt", 5555, 3333), "remote service ports must remain unique"),
        ),
    )
    def test_dynamic_forward_rejects_duplicate_service_attributes(self, addition, message):
        session = RemoteSession(self.request(), _FakeBackend())
        session.start()
        with pytest.raises(SessionError, match=message):
            session.forward((addition,))
        session.close()

    @pytest.mark.parametrize(
        ("services", "message"),
        (
            (
                (Service("gdb", 1234, 3333), Service("gdb", 1235, 6333)),
                "service names must be unique",
            ),
            (
                (Service("gdb", 1234, 3333), Service("tcl", 1234, 6333)),
                "local service ports must be unique",
            ),
            (
                (Service("gdb", 1234, 3333), Service("tcl", 1235, 3333)),
                "remote service ports must be unique",
            ),
        ),
    )
    def test_request_rejects_duplicate_service_attributes(self, services, message):
        with pytest.raises(ValueError, match=message):
            RemoteSessionRequest(
                "host",
                SshCommand(),
                services=services,
            )

    def test_dynamic_forward_failure_closes_session(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        session.start()
        backend.session.forward_error = RuntimeError("forward failed")
        with pytest.raises(RuntimeError, match="forward failed"):
            session.forward((Service("rtt", 5555, 5555),))
        assert session.state == SessionState.FAILED
        assert backend.session.actions[-1] == ("close",)

    def test_dynamic_forward_failure_preserves_error_when_cleanup_fails(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        session.start()
        forward_error = RuntimeError("forward failed")
        backend.session.forward_error = forward_error
        backend.session.close_error = RuntimeError("cleanup failed")

        with pytest.raises(RuntimeError, match="forward failed") as raised:
            session.forward((Service("rtt", 5555, 5555),))

        assert raised.value is forward_error
        assert any("cleanup failed" in note for note in raised.value.__notes__)
        assert session.state == SessionState.FAILED
        assert backend.session.actions[-1] == ("close",)

    def test_poll_failure_closes_session_and_marks_failed(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        session.start()
        backend.session.poll_error = RuntimeError("reader failed")
        with pytest.raises(RuntimeError, match="reader failed"):
            session.poll()
        assert session.state == SessionState.FAILED
        assert backend.session.actions[-1] == ("close",)

    def test_wait_failure_closes_session_and_marks_failed(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        session.start()
        backend.session.wait_error = RuntimeError("reader failed")
        with pytest.raises(RuntimeError, match="reader failed"):
            session.wait()
        assert session.state == SessionState.FAILED
        assert backend.session.actions[-1] == ("close",)

    def test_close_failure_keeps_session_retryable(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        session.start()
        cleanup_error = RuntimeError("cleanup failed")
        backend.session.close_error = cleanup_error

        with pytest.raises(RuntimeError, match="cleanup failed") as raised:
            session.close()

        assert raised.value is cleanup_error
        assert session.state == SessionState.FAILED
        backend.session.close_error = None
        session.close()
        assert session.state == SessionState.CLOSED
        assert [action for action in backend.session.actions if action == ("close",)] == [
            ("close",),
            ("close",),
        ]


class TestAllocation:
    def test_range_and_exhaustion(self):
        assert ipaddress.IPv4Address(random_loopback_address()) in LOOPBACK_RANGE
        calls = []

        def collision(address):
            calls.append(address)
            raise OSError("occupied")

        with pytest.raises(RuntimeError, match="3 attempts"):
            allocate_loopback(collision, attempts=3)
        assert len(calls) == 3


class TestFlashPlanning:
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
