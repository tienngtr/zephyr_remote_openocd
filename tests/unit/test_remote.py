from __future__ import annotations

import io
import ipaddress
from pathlib import Path
import tarfile
import tempfile
import unittest

from zephyr_remote_openocd.remote.model import (
    RemoteSessionRequest, Service, SessionAllocation, SessionDescriptor,
    SessionState, StagedFile,
)
from zephyr_remote_openocd.remote.protocol import EventOrder, ProtocolError, decode_message, encode_message
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


if __name__ == "__main__":
    unittest.main()
