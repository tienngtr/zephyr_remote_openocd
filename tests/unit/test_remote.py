# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import io
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.remote import rtt as rtt_module
from zephyr_remote_openocd.remote.backend import SshHelperSession
from zephyr_remote_openocd.remote.debug import (
    DebugInputs,
    DebugPlanError,
    build_debug_plan,
    parse_openocd_version,
    thread_info_enabled,
)
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.flash import FlashInputs, build_flash_plan
from zephyr_remote_openocd.remote.model import (
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
)
from zephyr_remote_openocd.remote.rtt import RttClientError, run_rtt_client
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
from zephyr_remote_openocd.remote.staging import StagingError, build_archive, extract_archive

from tests.support import ROOT


class ProtocolTests(unittest.TestCase):
    def test_round_trip_and_rejections(self):
        self.assertEqual(decode_message(encode_message("HELLO", value=3))["value"], 3)
        for invalid in (b"not-json\n", b"[]\n", b'{"version":2,"type":"HELLO"}\n'):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolError):
                decode_message(invalid)

    def test_event_order_is_enforced(self):
        order = EventOrder()
        with self.assertRaises(ProtocolError):
            order.accept(decode_message(encode_message("SERVICE_READY")))
        order.accept(decode_message(encode_message("HELLO")))
        order.accept(decode_message(encode_message("SESSION_CREATED")))
        order.accept(decode_message(encode_message("SERVICE_READY")))


class StagingTests(unittest.TestCase):
    def test_binary_and_empty_files_round_trip(self):
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
            output = root / "output"
            output.mkdir()
            _, _, files = extract_archive(archive.stream, output)
            archive.stream.close()
            self.assertEqual(files, ("a/empty", "b/binary"))
            self.assertEqual((output / "b/binary").read_bytes(), bytes(range(256)) + b"\0")

    def test_unsafe_archive_members_are_rejected(self):
        cases = (("../escape", None), ("absolute", "symlink"), ("fifo", "fifo"))
        for name, kind in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w") as archive:
                    info = tarfile.TarInfo("/absolute" if name == "absolute" else name)
                    if kind == "symlink":
                        info.type = tarfile.SYMTYPE
                        info.linkname = "target"
                    elif kind == "fifo":
                        info.type = tarfile.FIFOTYPE
                    else:
                        info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
                stream.seek(0)
                with self.assertRaises(StagingError):
                    extract_archive(stream, Path(directory))


class _FakeSession(BackendSession):
    def __init__(self):
        self.actions = []
        self.returncode = None
        self.forward_error = None

    def stage(self, files):
        self.actions.append(("stage", tuple(files)))

    def start(self, services):
        self.actions.append(("start", tuple(services)))
        return SessionDescriptor(SessionAllocation("id", "/workspace"), "127.64.0.1")

    def forward(self, services):
        self.actions.append(("forward", tuple(services)))
        if self.forward_error is not None:
            raise self.forward_error

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return 9

    def close(self):
        self.actions.append(("close",))


class _FakeBackend(SessionBackend):
    def __init__(self):
        self.session = _FakeSession()

    def create(self, request):
        return self.session


class SessionTests(unittest.TestCase):
    def request(self):
        return RemoteSessionRequest("host", SshCommand(), services=(Service("gdb", 1234, 3333),))

    def test_success_context_and_controller_loss(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        with session:
            self.assertEqual(session.state, SessionState.READY)
        self.assertEqual(session.state, SessionState.CLOSED)
        session = RemoteSession(self.request(), backend := _FakeBackend())
        session.start()
        backend.session.returncode = 7
        self.assertEqual(session.poll(), 7)
        self.assertEqual(session.termination_returncode, 7)
        self.assertEqual(session.state, SessionState.FAILED)

    def test_dynamic_forward_and_duplicate_rejection(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        session.start()
        rtt = Service("rtt", 5555, 5555)
        session.forward((rtt,))
        self.assertIn(("forward", (rtt,)), backend.session.actions)
        with self.assertRaisesRegex(SessionError, "service names must remain unique"):
            session.forward((rtt,))
        session.close()

    def test_dynamic_forward_failure_closes_session(self):
        backend = _FakeBackend()
        session = RemoteSession(self.request(), backend)
        session.start()
        backend.session.forward_error = RuntimeError("forward failed")
        with self.assertRaisesRegex(RuntimeError, "forward failed"):
            session.forward((Service("rtt", 5555, 5555),))
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(backend.session.actions[-1], ("close",))


class ForwardingLifecycleTests(unittest.TestCase):
    class Process:
        def __init__(self, returncode=None):
            self.returncode = returncode
            self.terminate_calls = 0
            self.kill_calls = 0
            self.stdin = None
            self.stdout = None
            self.stderr = io.BytesIO(b"bind failed")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminate_calls += 1
            self.returncode = 0

        def kill(self):
            self.kill_calls += 1
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    class Command:
        def __init__(self, process):
            self.process = process
            self.calls = []

        def popen(self, host, remote_command, *extra_args):
            self.calls.append((host, remote_command, extra_args))
            return self.process

    @staticmethod
    def session(command):
        session = object.__new__(SshHelperSession)
        session.request = RemoteSessionRequest("dot4", command)
        session.forward_start_timeout = 1
        session.forwards = []
        session.closed = False
        return session

    @staticmethod
    def port():
        try:
            listener = socket.socket()
        except PermissionError as error:
            raise unittest.SkipTest("sandbox prohibits loopback listeners") from error
        with listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def test_gdb_listener_readiness_does_not_probe_single_client_socket(self):
        process = self.Process()
        command = self.Command(process)
        session = self.session(command)
        service = Service("gdb", self.port(), 3333)
        with (
            patch.object(SshHelperSession, "_listener_ready", return_value=True),
            patch("zephyr_remote_openocd.remote.backend.socket.create_connection") as connect,
        ):
            session._start_forwards((service,), "127.64.1.1")
        connect.assert_not_called()
        session._close_forwards()
        self.assertEqual(process.terminate_calls, 1)

    def test_stale_gdb_forward_cannot_mask_current_forward_failure(self):
        class RaceProcess(self.Process):
            def __init__(self):
                super().__init__()
                self.poll_count = 0

            def poll(self):
                self.poll_count += 1
                return None if self.poll_count == 1 else 255

        stale = RaceProcess()
        command = self.Command(stale)
        session = self.session(command)
        service = Service("gdb", self.port(), 3333)
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", service.local_port))
            listener.listen()
        except OSError as error:
            listener.close()
            raise unittest.SkipTest("sandbox cannot create stale listener") from error
        with (
            patch("zephyr_remote_openocd.remote.backend.socket.create_connection") as connect,
            self.assertRaisesRegex(SessionError, "SSH forwarding failed"),
        ):
            try:
                session._start_forwards((service,), "127.64.1.1")
            finally:
                listener.close()
        connect.assert_not_called()
        session._close_forwards()
        self.assertEqual(stale.terminate_calls, 0)

    def test_forward_cleanup_is_reverse_order_and_idempotent(self):
        first = self.Process()
        second = self.Process()
        session = self.session(self.Command(first))
        session.forwards = [first, second]
        session._close_forwards()
        session._close_forwards()
        self.assertEqual(second.terminate_calls, 1)
        self.assertEqual(first.terminate_calls, 1)


class AllocationTests(unittest.TestCase):
    def test_range_and_exhaustion(self):
        self.assertIn(ipaddress.IPv4Address(random_loopback_address()), LOOPBACK_RANGE)
        calls = []

        def collision(address):
            calls.append(address)
            raise OSError("occupied")

        with self.assertRaisesRegex(RuntimeError, "32 attempts"):
            allocate_loopback(collision)
        self.assertEqual(len(calls), 32)


class FlashPlanningTests(unittest.TestCase):
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
            self.assertIn(f"bindto {ADDRESS_TOKEN}", argv)
            self.assertIn("gdb_port 7777", argv)
            self.assertNotIn("gdb_port disabled", argv)
            self.assertEqual(plan.process.environment, (("PROBE", "value"),))
            self.assertIn("{workspace}/staged/trees/search-0/board/openocd.cfg", argv)
            self.assertEqual(len([item for item in plan.staged_files if item.source == config]), 1)

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
            self.assertEqual(planned.remote, "/specific/image.hex")
            self.assertEqual(planner.remote_checks[0].path, "/specific/image.hex")

    def test_escaping_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            external = Path(outside) / "external.cfg"
            external.write_text("external")
            (root / "escape.cfg").symlink_to(external)
            with self.assertRaisesRegex(PathPlanningError, "escapes"):
                PathPlanner(()).plan_directory(root, "search-0")

    def test_bin_plan_requires_address_and_preserves_erase_verify(self):
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
            self.assertIn("mass_erase", joined)
            self.assertIn("program ", joined)
            self.assertIn("verify ", joined)
            self.assertIn("0x8000000", joined)

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
            self.assertLess(argv.index("set _ZEPHYR_BOARD_SERIAL ES-FT4232H-02"), argv.index("-f"))


class DebugPlanningTests(unittest.TestCase):
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
                    gdb_init=("monitor reset run", "quit"),
                ),
                PathPlanner(()),
            )
            self.assertEqual([item.name for item in debug.services], ["gdb", "tcl", "telnet"])
            self.assertEqual(
                debug.gdb_argv[-6:],
                (
                    "-ex",
                    "load",
                    "-ex",
                    "monitor reset run",
                    "-ex",
                    "quit",
                ),
            )
            self.assertIn("halt", debug.process.argv)
            attach = build_debug_plan(self.inputs(root, "attach"), PathPlanner(()))
            self.assertNotIn("load", attach.gdb_argv)
            server = build_debug_plan(
                self.inputs(
                    root,
                    "debugserver",
                    serial="probe",
                    reset_halt="reset init",
                ),
                PathPlanner(()),
            )
            self.assertIsNone(server.gdb_argv)
            self.assertIn("set _ZEPHYR_BOARD_SERIAL probe", server.process.argv)
            self.assertLess(
                server.process.argv.index("set _ZEPHYR_BOARD_SERIAL probe"),
                server.process.argv.index("-f"),
            )
            self.assertIn("reset init", server.process.argv)

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
            self.assertEqual(plan.services, (Service("gdb", 3355, 3344),))
            self.assertIn("target extended-remote 127.0.0.1:3355", plan.gdb_argv)
            self.assertIn("tcl_port disabled", plan.process.argv)
            with self.assertRaisesRegex(DebugPlanError, "gdb_port must be enabled"):
                build_debug_plan(self.inputs(Path(directory), gdb_port="disabled"), PathPlanner(()))

    def test_version_parsing_and_thread_info_decision(self):
        old = parse_openocd_version("Open On-Chip Debugger 0.11.0")
        development = parse_openocd_version("Open On-Chip Debugger 0.11.0+dev")
        current = parse_openocd_version("Open On-Chip Debugger 0.12.0-01050")
        self.assertFalse(thread_info_enabled(True, old))
        self.assertTrue(thread_info_enabled(True, development))
        self.assertTrue(thread_info_enabled(True, current))
        self.assertFalse(thread_info_enabled(False, None))
        with self.assertRaises(DebugPlanError):
            parse_openocd_version("unknown")
        with self.assertRaises(DebugPlanError):
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
            self.assertLess(
                argv.index("adapter speed 1000"), argv.index("$_TARGETNAME configure -rtos Zephyr")
            )
            self.assertTrue(plan.rtos_awareness)

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
            self.assertEqual([item.name for item in plan.services], ["gdb", "tcl", "telnet"])
            self.assertEqual(plan.rtt_service, Service("rtt", 5566, 5566))
            self.assertEqual(plan.rtt_setup, "batch-gdb")
            self.assertTrue(plan.launches_rtt_client)
            self.assertIn("--batch", plan.gdb_argv)
            self.assertLess(
                plan.gdb_argv.index("set pagination off"),
                plan.gdb_argv.index('monitor rtt setup 0x20001000 0x10 "SEGGER RTT"'),
            )
            self.assertIn("monitor rtt server start 5566 0", plan.gdb_argv)
            self.assertNotIn("rtt server start 5566 0", plan.process.argv)

    def test_rtt_server_is_ready_with_openocd_and_never_launches_client(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in ("debug", "debugserver"):
                with self.subTest(command=command):
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
                    self.assertEqual(plan.services[-1], Service("rtt", 5577, 5577))
                    self.assertIn("rtt server start 5577 0", plan.process.argv)
                    self.assertEqual(plan.rtt_setup, "openocd-startup")
                    self.assertFalse(plan.launches_rtt_client)

    def test_rtt_requires_control_block_and_enabled_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(DebugPlanError, "RTT control block not found"):
                build_debug_plan(self.inputs(root, "rtt"), PathPlanner(()))
            with self.assertRaisesRegex(DebugPlanError, "rtt_port must be enabled"):
                build_debug_plan(
                    self.inputs(root, "rtt", rtt_address=0x2000, rtt_port="disabled"),
                    PathPlanner(()),
                )
            with self.assertRaisesRegex(DebugPlanError, "rtt_port conflicts"):
                build_debug_plan(
                    self.inputs(root, "rtt", rtt_address=0x2000, rtt_port=3333),
                    PathPlanner(()),
                )


class RttClientTests(unittest.TestCase):
    @staticmethod
    def _listener(handler):
        try:
            listener = socket.socket()
        except PermissionError as error:
            raise unittest.SkipTest("sandbox prohibits loopback listeners") from error
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        thread = threading.Thread(target=handler, args=(listener,), daemon=True)
        thread.start()
        return listener.getsockname()[1], thread

    def test_bidirectional_non_tty_channel(self):
        received = []

        def server(listener):
            with listener, listener.accept()[0] as connection:
                connection.sendall(b"remote-output")
                received.append(connection.recv(64))

        port, thread = self._listener(server)
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        os.write(input_write, b"local-input")
        os.close(input_write)
        with (
            os.fdopen(input_read, "rb", buffering=0) as stdin,
            os.fdopen(output_write, "wb", buffering=0) as stdout,
        ):
            self.assertIsNone(run_rtt_client(port, lambda: None, stdin=stdin, stdout=stdout))
        thread.join(2)
        self.assertEqual(received, [b"local-input"])
        self.assertEqual(os.read(output_read, 64), b"remote-output")
        os.close(output_read)

    def test_immediate_forwarded_channel_failure_is_authoritative(self):
        def server(listener):
            with listener, listener.accept()[0]:
                pass

        port, thread = self._listener(server)
        with self.assertRaisesRegex(RttClientError, "remote channel"):
            run_rtt_client(port, lambda: None, startup_timeout=1)
        thread.join(2)

    def test_tty_preserves_signals_and_restores_complete_state(self):
        class Connection:
            def recv(self, _size):
                return b""

            def close(self):
                pass

        original = [
            1,
            2,
            3,
            rtt_module.termios.ICANON | rtt_module.termios.ECHO | rtt_module.termios.ISIG,
            5,
            6,
            [7],
        ]
        connection = Connection()
        with (
            tempfile.TemporaryFile("w+b") as stream,
            patch.object(rtt_module, "_connect", return_value=(connection, b"")),
            patch.object(rtt_module.os, "isatty", return_value=True),
            patch.object(rtt_module.os, "read", return_value=b""),
            patch.object(
                rtt_module.select,
                "select",
                side_effect=[([stream.fileno()], [], []), ([connection], [], [])],
            ),
            patch.object(
                rtt_module.termios,
                "tcgetattr",
                side_effect=[list(original), list(original)],
            ),
            patch.object(rtt_module.termios, "tcsetattr") as set_attributes,
        ):
            self.assertIsNone(run_rtt_client(5555, lambda: None, stdin=stream, stdout=stream))
        configured = set_attributes.call_args_list[0].args[2]
        self.assertFalse(configured[3] & rtt_module.termios.ICANON)
        self.assertFalse(configured[3] & rtt_module.termios.ECHO)
        self.assertTrue(configured[3] & rtt_module.termios.ISIG)
        self.assertEqual(set_attributes.call_args_list[-1].args[2], original)


class RealProcessHelperTests(unittest.TestCase):
    def test_persistent_process_requires_marker_and_connectable_service(self):
        try:
            probe = socket.socket()
            probe.bind(("127.64.0.1", 0))
            remote_port = probe.getsockname()[1]
            probe.close()
        except PermissionError:
            self.skipTest("sandbox prohibits loopback listeners")
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                self.assertEqual(json.loads(process.stdout.readline())["type"], "HELLO")
                json.loads(process.stdout.readline())
                marker = "ZRO_READY_unit"
                child_code = (
                    "import socket,sys,time;"
                    "s=socket.socket();s.bind((sys.argv[1],int(sys.argv[2])));s.listen();"
                    "print(sys.argv[3],flush=True);time.sleep(30)"
                )
                process.stdin.write(
                    encode_message(
                        "START_OPENOCD",
                        argv=[
                            sys.executable,
                            "-c",
                            child_code,
                            ADDRESS_TOKEN,
                            str(remote_port),
                            marker,
                        ],
                        environment={},
                        required_paths=[],
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        readiness_marker=marker,
                        readiness_timeout=5,
                    )
                )
                process.stdin.flush()
                events = []
                while not any(event["type"] == "SERVICE_READY" for event in events):
                    events.append(json.loads(process.stdout.readline()))
                self.assertEqual(events[0]["type"], "PROCESS_STARTED")
                self.assertTrue(
                    any(
                        event["type"] == "CHILD_OUTPUT" and event["payload"] == marker
                        for event in events
                    )
                )
                process.stdin.write(encode_message("STOP"))
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=8), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_helper_version_operation_is_structured(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        result = subprocess.run(
            [sys.executable, str(helper), "openocd-version", sys.executable],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        message = json.loads(result.stdout)
        self.assertEqual(message["type"], "OPENOCD_VERSION")
        self.assertIn("Python", message["output"])

    def test_output_exit_status_and_workspace_cleanup(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                self.assertEqual(json.loads(process.stdout.readline())["type"], "HELLO")
                created = json.loads(process.stdout.readline())
                command = [
                    sys.executable,
                    "-c",
                    'import sys;print("out");print("err",file=sys.stderr);sys.exit(7)',
                ]
                process.stdin.write(
                    encode_message("START_OPENOCD", argv=command, environment={}, required_paths=[])
                )
                process.stdin.flush()
                events = [json.loads(line) for line in process.stdout]
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(events[0]["type"], "PROCESS_STARTED")
                outputs = {
                    (event["stream"], event["payload"])
                    for event in events
                    if event["type"] == "CHILD_OUTPUT"
                }
                self.assertEqual(outputs, {("stdout", "out"), ("stderr", "err")})
                exit_event = next(event for event in events if event["type"] == "PROCESS_EXIT")
                self.assertEqual(exit_event["returncode"], 7)
                self.assertFalse(Path(created["remote_workspace"]).exists())
                self.assertEqual(process.stderr.read(), b"")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_controller_signal_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            child_pid = None
            workspace = None
            try:
                assert process.stdout is not None and process.stdin is not None
                self.assertEqual(json.loads(process.stdout.readline())["type"], "HELLO")
                created = json.loads(process.stdout.readline())
                workspace = Path(created["remote_workspace"])
                command = [sys.executable, "-c", "import time; time.sleep(30)"]
                process.stdin.write(
                    encode_message("START_OPENOCD", argv=command, environment={}, required_paths=[])
                )
                process.stdin.flush()
                started = json.loads(process.stdout.readline())
                self.assertEqual(started["type"], "PROCESS_STARTED")
                child_pid = started["child_pid"]
                process.terminate()
                self.assertEqual(process.wait(timeout=8), 0)
                self.assertFalse(workspace.exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_partial_openocd_start_cleans_child_and_workspace(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                self.assertEqual(json.loads(process.stdout.readline())["type"], "HELLO")
                created = json.loads(process.stdout.readline())
                workspace = Path(created["remote_workspace"])
                try:
                    listener = socket.socket()
                except PermissionError as error:
                    raise unittest.SkipTest("sandbox prohibits loopback listeners") from error
                with listener:
                    listener.bind(("127.0.0.1", 0))
                    remote_port = listener.getsockname()[1]
                marker = "ZRO_READY_partial"
                command = [
                    sys.executable,
                    "-c",
                    "import sys,time; print(sys.argv[1], flush=True); time.sleep(30)",
                    marker,
                ]
                process.stdin.write(
                    encode_message(
                        "START_OPENOCD",
                        argv=command,
                        environment={},
                        required_paths=[],
                        services=[{"name": "tcl", "remote_port": remote_port}],
                        readiness_marker=marker,
                        readiness_timeout=0.5,
                    )
                )
                process.stdin.flush()
                events = [json.loads(line) for line in process.stdout]
                self.assertEqual(events[0]["type"], "PROCESS_STARTED")
                self.assertTrue(any(event["type"] == "CHILD_OUTPUT" for event in events))
                self.assertEqual(process.wait(timeout=8), 0)
                self.assertFalse(workspace.exists())
                self.assertEqual(events[-1]["type"], "ERROR")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_backend_returns_child_status_and_relays_output(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand:
                argv_prefix = ("local-test",)

                def popen(inner, host, remote_command, *extra_args):  # pylint: disable=no-self-argument
                    return subprocess.Popen(
                        [sys.executable, str(helper), "control"],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )

                def run_stream(inner, host, remote_command, stream, timeout=60):  # pylint: disable=no-self-argument
                    workspace = remote_command.rsplit(" ", 1)[1]
                    return subprocess.run(
                        [sys.executable, str(helper), "stage", workspace],
                        env=environment,
                        input=stream.read(),
                        capture_output=True,
                        timeout=timeout,
                        check=False,
                    )

            output = []
            remote_process = RemoteProcess(
                "openocd",
                (sys.executable, "-c", 'import sys;print("hello");sys.exit(6)'),
            )
            request = RemoteSessionRequest("local", LocalCommand(), process=remote_process)
            backend = SshHelperSession(
                request,
                DeploymentResult(str(helper), "digest", False),
                0.1,
                lambda stream, payload: output.append((stream, payload)),
            )
            try:
                backend.stage(())
                descriptor = backend.start(())
                self.assertIn(ipaddress.ip_address(descriptor.remote_address), LOOPBACK_RANGE)
                self.assertEqual(backend.wait(5), 6)
                self.assertEqual(output, [("stdout", "hello")])
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
