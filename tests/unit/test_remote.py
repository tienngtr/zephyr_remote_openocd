from __future__ import annotations

import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import unittest

from tests.support import ROOT
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.remote.backend import SshHelperSession
from zephyr_remote_openocd.remote.deploy import DeploymentResult
from zephyr_remote_openocd.remote.flash import FlashInputs, build_flash_plan
from zephyr_remote_openocd.remote.model import (
    RemoteProcess, RemoteSessionRequest, Service, SessionAllocation, SessionDescriptor,
    SessionState, StagedFile,
)
from zephyr_remote_openocd.remote.protocol import EventOrder, ProtocolError, decode_message, encode_message
from zephyr_remote_openocd.remote.paths import ADDRESS_TOKEN, PathPlanner, PathPlanningError
from zephyr_remote_openocd.remote.services import LOOPBACK_RANGE, allocate_loopback, random_loopback_address
from zephyr_remote_openocd.remote.session import BackendSession, RemoteSession, SessionBackend
from zephyr_remote_openocd.remote.ssh import SshCommand
from zephyr_remote_openocd.remote.staging import StagingError, build_archive, extract_archive


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
            archive = build_archive((
                StagedFile(root / "empty", "a/empty"),
                StagedFile(root / "binary", "b/binary"),
            ), spool_limit=1)
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

    def stage(self, files): self.actions.append(("stage", tuple(files)))
    def start(self, services):
        self.actions.append(("start", tuple(services)))
        return SessionDescriptor(SessionAllocation("id", "/workspace"), "127.64.0.1")
    def poll(self): return self.returncode
    def wait(self, timeout=None): return 9
    def close(self): self.actions.append(("close",))


class _FakeBackend(SessionBackend):
    def __init__(self): self.session = _FakeSession()
    def create(self, request): return self.session


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
            plan = build_flash_plan(FlashInputs(
                executable="/opt/openocd", image_type="hex", file=None,
                elf_file=None, hex_file=str(image), bin_file=None,
                search_paths=(str(root / "scripts"),), config_files=(str(config),),
                load_command="flash write_image erase", verify_command="verify_image",
                pre_init=("gdb_port 7777", "tcl_port 8888", "telnet_port 9999"),
                verify=True,
            ), planner, (("PROBE", "value"),))
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
            planner = PathPlanner((
                PathMapping(root, PurePosixPath("/general")),
                PathMapping(specific, PurePosixPath("/specific")),
            ))
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
            plan = build_flash_plan(FlashInputs(
                executable="/openocd", image_type="bin", file=None,
                elf_file=None, hex_file=None, bin_file=str(image),
                search_paths=(), config_files=(), load_command="program",
                verify_command="verify", flash_address="0x8000000", erase=True,
                erase_commands=("mass_erase",), verify=True,
            ), PathPlanner(()))
            joined = "\n".join(plan.process.argv)
            self.assertIn("mass_erase", joined)
            self.assertIn("program ", joined)
            self.assertIn("verify ", joined)
            self.assertIn("0x8000000", joined)


class RealProcessHelperTests(unittest.TestCase):
    def test_output_exit_status_and_workspace_cleanup(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory
            process = subprocess.Popen(
                [sys.executable, str(helper), "control"], env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                self.assertEqual(json.loads(process.stdout.readline())["type"], "HELLO")
                created = json.loads(process.stdout.readline())
                command = [
                    sys.executable, "-c",
                    'import sys;print("out");print("err",file=sys.stderr);sys.exit(7)',
                ]
                process.stdin.write(encode_message(
                    "START_OPENOCD", argv=command, environment={}, required_paths=[]
                ))
                process.stdin.flush()
                events = [json.loads(line) for line in process.stdout]
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(events[0]["type"], "PROCESS_STARTED")
                outputs = {(event["stream"], event["payload"]) for event in events if event["type"] == "CHILD_OUTPUT"}
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

    def test_backend_returns_child_status_and_relays_output(self):
        helper = ROOT / "python/zephyr_remote_openocd/remote_helper.py"
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as directory:
            environment = os.environ.copy()
            environment["XDG_RUNTIME_DIR"] = directory

            class LocalCommand:
                argv_prefix = ("local-test",)

                def popen(inner, host, remote_command, *extra_args):
                    return subprocess.Popen(
                        [sys.executable, str(helper), "control"], env=environment,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )

                def run_stream(inner, host, remote_command, stream, timeout=60):
                    workspace = remote_command.rsplit(" ", 1)[1]
                    return subprocess.run(
                        [sys.executable, str(helper), "stage", workspace], env=environment,
                        input=stream.read(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=timeout, check=False,
                    )

            output = []
            remote_process = RemoteProcess(
                "openocd",
                (sys.executable, "-c", 'import sys;print("hello");sys.exit(6)'),
            )
            request = RemoteSessionRequest("local", LocalCommand(), process=remote_process)
            backend = SshHelperSession(
                request, DeploymentResult(str(helper), "digest", False), 0.1,
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
