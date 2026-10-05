#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Protocol v1 remote helper. This file is deliberately self-contained."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import contextvars
import errno
import fcntl
import hashlib
import io
import ipaddress
import json
import math
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import deque
from collections.abc import Coroutine, Iterable, Iterator
from contextlib import contextmanager, suppress
from enum import Enum, auto
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import IO, Any, NamedTuple

VERSION = 1
RANGE = ipaddress.IPv4Network("127.64.0.0/10")
SESSION_LOCK = ".session.lock"
STALE_SESSION_AGE = 24 * 60 * 60
WORKSPACE_LEASE_TIMEOUT = 5
WORKSPACE_LEASE_POLL_INTERVAL = 0.05
CHILD_TERM_TIMEOUT = 5
CHILD_POLL_INTERVAL = 0.05
CHILD_REAP_TIMEOUT = 1
CHILD_RELAY_JOIN_TIMEOUT = 2
RELAY_CHUNK_SIZE = 64 * 1024
# Keep in sync with remote/protocol.py; this file is deployed standalone.
MAX_CONTROL_FRAME_SIZE = 1024 * 1024
MAX_SESSION_ID_ATTEMPTS = 32
MAX_ADDRESS_ALLOCATION_ATTEMPTS = 32
# 18 random bytes provide 144 bits of entropy in a compact URL-safe ID.
SESSION_ID_RANDOM_BYTES = 18
MAX_CAPTURED_STARTUP_FRAGMENTS = 128
MAX_PENDING_OBSERVATIONS = 64
MAX_PROTOCOL_OUTPUT_BYTES = 16 * 1024 * 1024
_protocol_output: contextvars.ContextVar[_ProtocolOutput | None] = contextvars.ContextVar(
    "protocol_output", default=None
)


def _raise_cleanup_errors(errors):
    """Raise the first cleanup error after retaining subsequent diagnostics."""
    if not errors:
        return
    first, *additional = errors
    for error in additional:
        first.add_note(f"additional cleanup failure: {error}")
    raise first


def emit(kind, **values):
    line = json.dumps(
        {"version": VERSION, "type": kind, **values}, separators=(",", ":"), sort_keys=True
    )
    output = _protocol_output.get()
    if output is None:
        print(line, flush=True)
    else:
        output.enqueue((line + "\n").encode("utf-8"))


def error(message, code="HELPER_ERROR"):
    emit("ERROR", code=code, message=str(message))


def workspace_root():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / "zephyr_remote_openocd"
    return Path.home() / ".cache" / "zephyr_remote_openocd" / "sessions"


def _lease_path(work: Path) -> Path:
    return work.parent / f".{work.name}.lease"


def _closure_path(work: Path) -> Path:
    return work.parent / f".{work.name}.closed"


@contextmanager
def _stage_lease(work: Path) -> Iterator[None]:
    # Acquire ownership before checking closure. If cleanup wins before this
    # check, reject; if it wins afterwards, our shared lease prevents removal.
    with _lease_path(work).open("a+b") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("workspace cleanup has begun") from None
        if _closure_path(work).exists() or not work.is_dir():
            raise ValueError("workspace is not an active helper session")
        yield


def remove_workspace(work: Path) -> None:
    """Close admission before waiting for all admitted stages to release ownership."""
    # O_CREAT publishes closure atomically without depending on any lock owner.
    # Retain both sibling files after removal so pending stage descriptors and
    # late callers observe the same lease identity and closed admission.
    with _closure_path(work).open("ab"):
        pass
    with _lease_path(work).open("a+b") as lease:
        deadline = time.monotonic() + WORKSPACE_LEASE_TIMEOUT
        while True:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("staging did not release its workspace lease") from None
                time.sleep(WORKSPACE_LEASE_POLL_INTERVAL)
        try:
            shutil.rmtree(work)
        except FileNotFoundError:
            if work.exists():
                raise


def reclaim_stale_workspaces(root, now=None):
    """Remove old workspaces whose owning helper no longer holds its lock."""
    cutoff = (time.time() if now is None else now) - STALE_SESSION_AGE
    try:
        candidates = tuple(root.iterdir())
    except FileNotFoundError:
        return
    for path in candidates:
        lock_path = path / SESSION_LOCK
        try:
            if path.stat().st_mtime > cutoff:
                continue
            if path.is_file() and path.name.startswith("."):
                for suffix in (".lease", ".closed"):
                    if path.name.endswith(suffix):
                        # Metadata is retired only after its workspace is gone.
                        # A pending stage still checks closure and workspace
                        # existence after locking, including on a retired inode.
                        if not (root / path.name[1 : -len(suffix)]).exists():
                            path.unlink()
                        break
                continue
            if not path.is_dir():
                continue
            if not lock_path.is_file():
                with suppress(OSError):
                    remove_workspace(path)
                continue
            lock = lock_path.open("r+b")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            with suppress(OSError):
                remove_workspace(path)
        finally:
            lock.close()


def new_workspace():
    root = workspace_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    reclaim_stale_workspaces(root)
    for _ in range(MAX_SESSION_ID_ATTEMPTS):
        session_id = secrets.token_urlsafe(SESSION_ID_RANDOM_BYTES)
        path = root / session_id
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        lock = None
        try:
            lock = (path / SESSION_LOCK).open("xb")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (path / "staged").mkdir(mode=0o700)
            return session_id, path, lock
        except BaseException:
            if lock is not None:
                lock.close()
            shutil.rmtree(path, ignore_errors=True)
            raise
    raise RuntimeError("could not allocate an unpredictable session directory")


def valid_member(member, seen):
    name = member.name
    if member.isdir() and name.endswith("/"):
        name = name[:-1]
    parts = name.split("/") if name else []
    if not name or name == "." or any(p in ("", ".", "..") for p in parts) or "\0" in name:
        raise ValueError(f"unsafe archive path: {member.name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name:
        raise ValueError(f"unsafe archive path: {member.name!r}")
    if path in seen:
        raise ValueError(f"duplicate archive path: {path}")
    seen.add(path)
    if member.isdir():
        if member.size:
            raise ValueError(f"archive directory has content: {path}")
    elif not member.isreg():
        raise ValueError(f"archive member is not a regular file or directory: {path}")
    return path


def stage(workspace):
    root = workspace_root().resolve()
    work = Path(workspace).resolve()
    if root not in work.parents or work.parent != root or not work.is_dir():
        raise ValueError("workspace is not an active helper session")
    target_root = work / "staged"
    count = 0
    digest = hashlib.sha256()
    names = []
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        shutil.copyfileobj(sys.stdin.buffer, spool, length=1024 * 1024)
        with _stage_lease(work):
            spool.seek(0)
            with tarfile.open(fileobj=spool, mode="r:*") as archive:
                members = archive.getmembers()
                seen: set[PurePosixPath] = set()
                validated = []
                kinds = {}
                for member in members:
                    relative = valid_member(member, seen)
                    kind = "directory" if member.isdir() else "file"
                    kinds[relative] = kind
                    target = target_root.joinpath(*relative.parts)
                    if target_root.resolve() not in target.resolve().parents:
                        raise ValueError(f"archive path escapes staging directory: {relative}")
                    validated.append((member, relative, target, kind))
                if any(
                    kind == "file" and any(path in other.parents for other in kinds)
                    for path, kind in kinds.items()
                ):
                    raise ValueError("archive contains a file/directory ancestor conflict")
                for member, relative, target, kind in validated:
                    if kind == "directory":
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        # Keep extracted directories owner-private and writable so
                        # session cleanup can remove their contents regardless of
                        # archive permission metadata.
                        os.chmod(target, 0o700)
                        continue
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"missing archive content: {relative}")
                    with target.open("wb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            output.write(chunk)
                            count += len(chunk)
                            digest.update(chunk)
                    os.chmod(target, member.mode & 0o700 or 0o600)
                    names.append(str(relative))
            emit(
                "STAGED",
                byte_count=count,
                sha256=digest.hexdigest(),
                files=names,
                directories=[
                    str(relative) for _, relative, _, kind in validated if kind == "directory"
                ],
            )
    finally:
        spool.close()


def random_address():
    # Exclude network/broadcast endpoints without material bias.
    return str(
        ipaddress.IPv4Address(
            int(RANGE.network_address) + 1 + secrets.randbelow(RANGE.num_addresses - 2)
        )
    )


class _AllocatedAddress(str):
    """An allocated address with a lease held for the session lifetime."""

    lease: socket.socket

    def __new__(cls, address, lease):
        value = super().__new__(cls, address)
        value.lease = lease
        return value


class _RequiredOutputSentinels:
    """Session-owned state of required complete output lines."""

    def __init__(self, required_output_sentinels: Iterable[str]) -> None:
        self._unseen = set(required_output_sentinels)

    @property
    def ready(self) -> bool:
        return not self._unseen

    @property
    def unseen(self) -> tuple[str, ...]:
        return tuple(self._unseen)

    def observe(self, sentinel: str) -> None:
        self._unseen.discard(sentinel)


class _SentinelMatcher:
    """Recognize required startup output markers as complete trimmed lines."""

    def __init__(self, required_output_sentinels: _RequiredOutputSentinels) -> None:
        self.required_output_sentinels = required_output_sentinels
        self._reset_line()

    def _reset_line(self):
        self._candidates = {sentinel: 0 for sentinel in self.required_output_sentinels.unseen}
        self._started = False

    def feed(self, character):
        if self.required_output_sentinels.ready or not self._candidates:
            return
        if not self._started and character.isspace():
            return
        self._started = True
        for sentinel, index in tuple(self._candidates.items()):
            if index < len(sentinel) and character == sentinel[index]:
                self._candidates[sentinel] = index + 1
            elif index == len(sentinel) and character.isspace():
                continue
            else:
                del self._candidates[sentinel]

    def finish_line(self):
        for sentinel, index in self._candidates.items():
            if self._started and index == len(sentinel):
                self.required_output_sentinels.observe(sentinel)
        self._reset_line()


class _CapturedFragment(NamedTuple):
    """Retained startup output with its stream and boundary metadata."""

    stream: str
    payload: str
    line_end: bool


class _OutputDecoder:
    """Decode one observed stream, preserving fragments and sentinel lines."""

    def __init__(
        self, stream_name: str, required_output_sentinels: _RequiredOutputSentinels
    ) -> None:
        self.stream_name = stream_name
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.matcher = _SentinelMatcher(required_output_sentinels)
        self.pending: list[str] = []

    def feed(self, chunk: bytes) -> list[_CapturedFragment]:
        fragments = []

        def fragment(line_end=False):
            fragments.append(_CapturedFragment(self.stream_name, "".join(self.pending), line_end))
            self.pending.clear()

        for character in self.decoder.decode(chunk, final=not chunk):
            if character == "\n":
                self.matcher.finish_line()
                fragment(line_end=True)
                continue
            self.matcher.feed(character)
            self.pending.append(character)
            if len(self.pending) >= RELAY_CHUNK_SIZE:
                fragment()
        if not chunk:
            self.matcher.finish_line()
        if self.pending:
            fragment()
        return fragments


def is_bind_collision(output: list[_CapturedFragment]):
    """Recognize the POSIX EADDRINUSE diagnostic from OpenOCD startup."""
    phrase = "address already in use"
    lines: dict[str, str] = {}
    for item in output:
        stream = item.stream
        payload = item.payload
        line_end = item.line_end
        line = lines.get(stream, "") + payload.casefold()
        if phrase in line:
            return True
        lines[stream] = "" if line_end else line[-len(phrase) + 1 :]
    return False


def allocate_service_address(ports):
    for _ in range(MAX_ADDRESS_ALLOCATION_ATTEMPTS):
        address = random_address()
        # Abstract sockets share the TCP network namespace across remote users
        # and runtime roots. Keep the name independent of helper revisions.
        lease = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            lease.bind(f"\0zephyr_remote_openocd.address.{address}")
        except OSError as exc:
            lease.close()
            if exc.errno == errno.EADDRINUSE:
                continue
            raise
        sockets = []
        try:
            for port in ports:
                candidate = socket.socket()
                sockets.append(candidate)
                candidate.bind((address, port))
            return _AllocatedAddress(address, lease)
        except OSError:
            lease.close()
        except BaseException:
            lease.close()
            raise
        finally:
            for candidate in sockets:
                candidate.close()
    raise RuntimeError(
        f"loopback allocation exhausted after {MAX_ADDRESS_ALLOCATION_ATTEMPTS} attempts"
    )


def openocd_version(argv):
    if (
        not argv
        or not isinstance(argv[0], str)
        or not argv[0]
        or not all(isinstance(item, str) for item in argv[1:])
    ):
        raise ValueError("OpenOCD command must be a non-empty argv")
    result = subprocess.run(
        [*argv, "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode:
        raise RuntimeError(f"OpenOCD version query failed ({result.returncode}): {output.strip()}")
    emit("OPENOCD_VERSION", output=output)


def _protocol_kind(message):
    if (
        not isinstance(message, dict)
        or not isinstance(message.get("version"), int)
        or isinstance(message.get("version"), bool)
        or message["version"] != VERSION
    ):
        raise ValueError("incompatible or missing protocol version")
    kind = message.get("type")
    if not isinstance(kind, str) or not kind:
        raise ValueError("missing protocol command type")
    return kind


def _valid_service(item):
    return (
        isinstance(item, dict)
        and set(item) == {"name", "remote_port"}
        and isinstance(item.get("name"), str)
        and bool(item.get("name"))
        and isinstance(item.get("remote_port"), int)
        and not isinstance(item.get("remote_port"), bool)
        and 1 <= item["remote_port"] <= 65535
    )


def _parse_services(services, label):
    if not isinstance(services, list):
        raise ValueError(f"{label} services are invalid")
    if not all(_valid_service(item) for item in services):
        raise ValueError(f"{label} services are invalid")
    ports = [item["remote_port"] for item in services]
    names = [item["name"] for item in services]
    if len(ports) != len(set(ports)):
        raise ValueError(f"{label} services must use unique remote ports")
    if len(names) != len(set(names)):
        raise ValueError(f"{label} services must use unique names")
    return tuple(ServiceRequest(item["name"], item["remote_port"]) for item in services)


def _validate_argv(argv):
    if not isinstance(argv, list) or not argv:
        raise ValueError("START requires a non-empty string argv")
    if not isinstance(argv[0], str) or not argv[0]:
        raise ValueError("START requires a non-empty string argv")
    if not all(isinstance(arg, str) for arg in argv[1:]):
        raise ValueError("START requires a non-empty string argv")


def _validate_environment(environment):
    if not isinstance(environment, dict):
        raise ValueError("START environment must contain valid string values")
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise ValueError("START environment must contain valid string values")


def _validate_required_output_sentinels(sentinels):
    if not isinstance(sentinels, list) or not all(
        isinstance(sentinel, str)
        and sentinel
        and sentinel == sentinel.strip()
        and "\0" not in sentinel
        and "\n" not in sentinel
        and "\r" not in sentinel
        for sentinel in sentinels
    ):
        raise ValueError("START required output markers are invalid")
    if len(sentinels) != len(set(sentinels)):
        raise ValueError("START required output markers must be unique")


def _validate_timeout(timeout):
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("START readiness options are invalid")


def _validate_literal_prefix(literal_prefix, argv_length):
    if (
        isinstance(literal_prefix, bool)
        or not isinstance(literal_prefix, int)
        or not 0 <= literal_prefix <= argv_length
    ):
        raise ValueError("START readiness options are invalid")


def _validate_options(sentinels, timeout, literal_prefix, argv_length):
    _validate_required_output_sentinels(sentinels)
    _validate_timeout(timeout)
    _validate_literal_prefix(literal_prefix, argv_length)


def _expand(value, replacements):
    for placeholder, replacement in replacements.items():
        value = value.replace(placeholder, replacement)
    return value


def _check_required_paths(checks, replacements):
    for check in checks:
        candidate = Path(_expand(check.path, replacements))
        valid = candidate.is_file() if check.kind == "file" else candidate.is_dir()
        if not valid:
            raise ValueError(f"required remote {check.kind} is missing: {candidate}")


class ServiceRequest(NamedTuple):
    """Validated service data from one START request."""

    name: str
    remote_port: int


class RequiredPath(NamedTuple):
    kind: str
    path: str


class StartRequest(NamedTuple):
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    required_paths: tuple[RequiredPath, ...]
    services: tuple[ServiceRequest, ...]
    required_output_sentinels: tuple[str, ...]
    readiness_timeout: float
    literal_prefix: int


class StopRequest:
    """Request representing the parameterless STOP command."""


def _parse_required_paths(values):
    if not isinstance(values, list):
        raise ValueError("invalid required-path assertion")
    return tuple(_parse_required_path(item) for item in values)


def _parse_required_path(item):
    if (
        not isinstance(item, dict)
        or set(item) != {"kind", "path"}
        or item.get("kind") not in ("file", "directory")
        or not isinstance(item.get("path"), str)
        or not item["path"]
        or "\0" in item["path"]
    ):
        raise ValueError("invalid required-path assertion")
    return RequiredPath(item["kind"], item["path"])


def _decode_start(message):
    fields = {
        "version",
        "type",
        "argv",
        "environment",
        "required_paths",
        "services",
        "required_output_sentinels",
        "readiness_timeout",
        "literal_prefix",
    }
    if set(message) != fields:
        raise ValueError("START fields are invalid")
    argv = message["argv"]
    environment = message["environment"]
    sentinels = message["required_output_sentinels"]
    timeout = message["readiness_timeout"]
    literal_prefix = message["literal_prefix"]
    _validate_argv(argv)
    _validate_environment(environment)
    _validate_options(sentinels, timeout, literal_prefix, len(argv))
    services = _parse_services(message["services"], "START")
    checks = _parse_required_paths(message["required_paths"])
    return StartRequest(
        tuple(argv),
        tuple(environment.items()),
        checks,
        services,
        tuple(sentinels),
        float(timeout),
        literal_prefix,
    )


def decode_command(message):
    """Decode and validate one control command into an immutable request."""
    kind = _protocol_kind(message)
    if kind == "START":
        return _decode_start(message)
    if kind == "STOP":
        if set(message) != {"version", "type"}:
            raise ValueError("STOP fields are invalid")
        return StopRequest()
    raise ValueError(f"unexpected command: {kind!r}")


async def _readable(descriptor: int) -> None:
    """Wait for fd readiness without introducing a separately buffered reader."""
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def observed():
        if not ready.done():
            ready.set_result(None)

    loop.add_reader(descriptor, observed)
    try:
        await ready
    finally:
        loop.remove_reader(descriptor)


class _ProtocolOutput:
    """Session-owned nonblocking JSON output with bounded buffering and cleanup."""

    def __init__(self):
        self.stream = sys.stdout
        try:
            self.descriptor: int | None = self.stream.fileno()
        except (AttributeError, io.UnsupportedOperation):
            # In-memory callers have no Unix fd or external backpressure.
            self.descriptor = None
        self.frames: deque[bytes] = deque()
        self.pending_bytes = 0
        self.offset = 0
        self.available = asyncio.Event()
        self.empty = asyncio.Event()
        self.empty.set()
        self.failure: Exception | None = None
        self.was_blocking = None
        if self.descriptor is not None:
            self.was_blocking = os.get_blocking(self.descriptor)
            os.set_blocking(self.descriptor, False)

    def enqueue(self, frame: bytes) -> None:
        if self.failure is not None:
            raise self.failure
        if self.descriptor is None:
            self.stream.write(frame.decode("utf-8"))
            self.stream.flush()
            return
        if self.pending_bytes + len(frame) > MAX_PROTOCOL_OUTPUT_BYTES:
            raise BufferError("protocol output backlog exceeded its bound")
        self.frames.append(frame)
        self.pending_bytes += len(frame)
        self.empty.clear()
        self.available.set()

    async def _writable(self) -> None:
        assert self.descriptor is not None
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def observed():
            if not ready.done():
                ready.set_result(None)

        loop.add_writer(self.descriptor, observed)
        try:
            await ready
        finally:
            loop.remove_writer(self.descriptor)

    async def run(self) -> None:
        assert self.descriptor is not None
        try:
            while True:
                await self.available.wait()
                while self.frames:
                    frame = self.frames[0]
                    try:
                        count = os.write(self.descriptor, memoryview(frame)[self.offset :])
                    except BlockingIOError:
                        await self._writable()
                        continue
                    self.offset += count
                    self.pending_bytes -= count
                    if self.offset == len(frame):
                        self.frames.popleft()
                        self.offset = 0
                self.available.clear()
                self.empty.set()
        except Exception as exc:
            self.failure = exc
            self.empty.set()
            raise

    async def drain(self) -> None:
        try:
            async with asyncio.timeout(CHILD_RELAY_JOIN_TIMEOUT):
                await self.empty.wait()
        except TimeoutError as exc:
            raise RuntimeError("protocol output did not drain before cleanup deadline") from exc
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        if self.descriptor is not None and self.was_blocking is not None:
            os.set_blocking(self.descriptor, self.was_blocking)
        self.frames.clear()


class _AsyncInput:
    """One cancellable raw observation path for an owned input descriptor."""

    def __init__(self, descriptor: int):
        self.descriptor = descriptor
        self.was_blocking = os.get_blocking(descriptor)
        self._wake: asyncio.Future[None] | None = None
        self._checkpoint: asyncio.Future[None] | None = None
        self._closed = False
        os.set_blocking(descriptor, False)

    def _observed(self) -> None:
        if self._checkpoint is not None and not self._checkpoint.done():
            self._checkpoint.set_result(None)

    def _wake_reader(self) -> None:
        if self._wake is not None and not self._wake.done():
            self._wake.set_result(None)

    def checkpoint(self) -> asyncio.Future[None]:
        """Request one final raw observation by this reader, without a competitor."""
        self._checkpoint = asyncio.get_running_loop().create_future()
        if self._closed:
            self._observed()
        else:
            self._wake_reader()
        return self._checkpoint

    async def read(self) -> bytes:
        while True:
            try:
                chunk = os.read(self.descriptor, RELAY_CHUNK_SIZE)
            except BlockingIOError:
                self._observed()
                loop = asyncio.get_running_loop()
                self._wake = loop.create_future()
                loop.add_reader(self.descriptor, self._wake_reader)
                try:
                    await self._wake
                finally:
                    loop.remove_reader(self.descriptor)
                    self._wake = None
            else:
                self._observed()
                return chunk

    def close(self) -> None:
        self._closed = True
        self._observed()
        os.set_blocking(self.descriptor, self.was_blocking)


class SupervisedChild:
    """Own a process group; observe the leader before explicitly reaping it."""

    def __init__(
        self, process: subprocess.Popen[bytes], required_output_sentinels: Iterable[str] = ()
    ) -> None:
        self.process = process
        self.required_output_sentinels = _RequiredOutputSentinels(required_output_sentinels)
        self.startup_output: list[_CapturedFragment] = []
        self.decoders = {
            name: _OutputDecoder(name, self.required_output_sentinels)
            for name in ("stdout", "stderr")
        }
        self.output_finished: set[str] = set()
        self.readers: dict[str, _AsyncInput] = {}
        self.tasks: list[asyncio.Task[_ObservationFailed | None]] = []
        self._observed_returncode: int | None = None

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        if self._observed_returncode is not None:
            return self._observed_returncode
        return self.process.returncode

    def poll(self) -> int | None:
        if self._observed_returncode is not None:
            return self._observed_returncode
        if self.process.returncode is not None:
            self._observed_returncode = self.process.returncode
            return self._observed_returncode
        result = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None or result.si_pid == 0:
            return None
        if result.si_code == os.CLD_EXITED:
            returncode = result.si_status
        elif result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
            returncode = -result.si_status
        else:
            raise RuntimeError(f"unexpected child wait status: {result.si_code}")
        self._observed_returncode = returncode
        return returncode

    async def wait_for_exit(self) -> int:
        returncode = self.poll()
        if returncode is not None:
            return returncode
        try:
            descriptor = os.pidfd_open(self.pid)
        except OSError:
            # Older supported Linux kernels may not provide pidfds.
            while self.poll() is None:
                await asyncio.sleep(CHILD_POLL_INTERVAL)
        else:
            try:
                await _readable(descriptor)
            finally:
                os.close(descriptor)
        returncode = self.poll()
        assert returncode is not None
        return returncode

    async def _wait_for_leader_exit(self) -> bool:
        try:
            async with asyncio.timeout(CHILD_TERM_TIMEOUT):
                await self.wait_for_exit()
        except TimeoutError:
            return False
        return True

    def _group_exists(self):
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _remaining_group_members(self):
        """Return observable non-leader members of the owned process group."""
        members = []
        try:
            entries = Path("/proc").iterdir()
        except OSError:
            return ()
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == self.pid:
                continue
            try:
                fields = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
                if int(fields[2]) == self.pid:
                    members.append(int(entry.name))
            except (IndexError, OSError, ValueError):
                continue
        return tuple(members)

    def _warn_remaining_group_members(self):
        members = self._remaining_group_members()
        if members:
            message = (
                "warning: terminating remaining OpenOCD process-group members: "
                + ", ".join(map(str, members))
                + "\n"
            )
            try:
                descriptor = sys.stderr.fileno()
            except (AttributeError, io.UnsupportedOperation):
                sys.stderr.write(message)
                sys.stderr.flush()
                return
            was_blocking = os.get_blocking(descriptor)
            try:
                os.set_blocking(descriptor, False)
                with suppress(BlockingIOError):
                    os.write(descriptor, message.encode("utf-8"))
            finally:
                os.set_blocking(descriptor, was_blocking)

    async def terminate(self) -> None:
        errors = []
        if self.process.returncode is None:
            group_exists = True
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                group_exists = False
            except Exception as exc:
                errors.append(exc)
            if group_exists:
                try:
                    await self._wait_for_leader_exit()
                except Exception as exc:
                    errors.append(exc)
                try:
                    group_exists = self._group_exists()
                except Exception as exc:
                    errors.append(exc)
                    group_exists = True
                if group_exists:
                    with suppress(Exception):
                        self._warn_remaining_group_members()
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except Exception as exc:
                        errors.append(exc)
            try:
                # Reap only after group signalling; this wait has a finite budget.
                self._observed_returncode = self.process.wait(timeout=CHILD_REAP_TIMEOUT)
            except Exception as exc:
                errors.append(exc)
        _raise_cleanup_errors(errors)

    def close_streams(self) -> None:
        errors = []
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except Exception as exc:
                    errors.append(exc)
        _raise_cleanup_errors(errors)


def _rollback_spawned_process(process):
    errors = []
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except BaseException as error:
        errors.append(error)
    try:
        process.wait(timeout=CHILD_REAP_TIMEOUT)
    except BaseException as error:
        errors.append(error)
    for stream in (process.stdout, process.stderr):
        if stream is None or stream.closed:
            continue
        try:
            stream.close()
        except BaseException as error:
            errors.append(error)
    _raise_cleanup_errors(errors)


def _spawn_child(argv, *, cwd=None, environment=None, required_output_sentinels=()):
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        return SupervisedChild(process, required_output_sentinels)
    except BaseException as error:
        try:
            _rollback_spawned_process(process)
        except BaseException as cleanup_error:
            error.add_note(f"child ownership rollback also failed: {cleanup_error}")
        raise


def materialize_argv(
    argv: Iterable[str], *, workspace: str, address: str, literal_prefix: int = 0
) -> tuple[str, ...]:
    """Resolve session placeholders while preserving the configured literal prefix.

    The helper reports this exact argv before spawning each child attempt so
    diagnostics never reconstruct session expansion independently.
    """
    replacements = {"{workspace}": workspace, "{address}": address}
    return tuple(
        arg if index < literal_prefix else _expand(arg, replacements)
        for index, arg in enumerate(argv)
    )


def _child_environment(request):
    environment = os.environ.copy()
    environment.update(dict(request.environment))
    return environment


class _ControlFrames:
    """Incrementally frame bounded LF-delimited control input."""

    def __init__(self):
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    def pop_frame(self) -> bytes | None:
        delimiter = self._buffer.find(b"\n")
        size = delimiter + 1 if delimiter >= 0 else len(self._buffer) + 1
        if size > MAX_CONTROL_FRAME_SIZE:
            raise ValueError("protocol frame exceeds maximum size")
        if delimiter < 0:
            return None
        frame = bytes(self._buffer[: delimiter + 1])
        del self._buffer[: delimiter + 1]
        return frame

    def finish(self) -> None:
        if self._buffer:
            raise ValueError("protocol frame is missing its LF delimiter")


class _State(Enum):
    CREATED = auto()
    STARTING = auto()
    ACTIVE = auto()
    TERMINATING = auto()
    CLOSED = auto()


class _ControlFrame(NamedTuple):
    frame: bytes


class _ControlEOF(NamedTuple):
    pass


class _ChildOutput(NamedTuple):
    child: SupervisedChild
    stream: str
    chunk: bytes


class _ChildExited(NamedTuple):
    child: SupervisedChild
    returncode: int


class _Deadline(NamedTuple):
    child: SupervisedChild
    readiness: bool
    final: bool = False


class _SignalReceived(NamedTuple):
    signum: int


class _ObservationFailed(NamedTuple):
    source: str
    exception: Exception
    child: SupervisedChild | None = None


class _GroupCleaned(NamedTuple):
    child: SupervisedChild
    exception: Exception | None


_Observation = (
    _ControlFrame
    | _ControlEOF
    | _ChildOutput
    | _ChildExited
    | _Deadline
    | _SignalReceived
    | _ObservationFailed
    | _GroupCleaned
)


class ControlSession:
    """Sole lifecycle owner of one structured remote-helper session."""

    def __init__(self, session_id: str, work: Path, workspace_lock: IO[bytes]) -> None:
        self.session_id = session_id
        self.work = work
        self.workspace_lock = workspace_lock
        self.child: SupervisedChild | None = None
        self.protocol_error: BaseException | None = None
        self.operation_error: BaseException | None = None
        self.cleanup_errors: list[BaseException] = []
        self.state = _State.CREATED
        self.ending = False
        self.close_reason: str | None = None
        self.natural_returncode: int | None = None
        self.request: StartRequest | None = None
        self.attempt = 0
        self.address = ""
        self._address_lease: socket.socket | None = None
        self._group_cleaned = False
        self._startup_exit: int | None = None
        self._deadline_task: asyncio.Task[_ObservationFailed | None] | None = None
        self._events: asyncio.Queue[_Observation] = asyncio.Queue(MAX_PENDING_OBSERVATIONS)
        self._signals: asyncio.Queue[int] = asyncio.Queue(1)
        self._pending_signum: int | None = None
        self._tasks: asyncio.TaskGroup | None = None
        self._session_tasks: list[asyncio.Task[_ObservationFailed | None]] = []
        self._resources_released = False
        self._output_available = True
        self._control_reader: _AsyncInput | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def create(cls) -> ControlSession:
        return cls(*new_workspace())

    def announce(self) -> None:
        emit(
            "SESSION_CREATED",
            helper="zephyr_remote_openocd",
            session_id=self.session_id,
            remote_workspace=str(self.work),
        )

    def handle_signal(self, signum: int = signal.SIGTERM, _frame: FrameType | None = None) -> None:
        # Latch only plain state here: asyncio primitives are not safe to
        # mutate from reentrant Unix signal-handler context.
        if self._pending_signum is not None:
            return
        self._pending_signum = signum
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._enqueue_signal, signum)

    def _enqueue_signal(self, signum: int) -> None:
        # This callback runs on the loop, after the signal handler returns.
        with suppress(asyncio.QueueFull):
            self._signals.put_nowait(signum)

    async def _observe_signals(self) -> None:
        while True:
            await self._events.put(_SignalReceived(await self._signals.get()))

    async def _observe_control(self) -> None:
        frames = _ControlFrames()
        reader = _AsyncInput(sys.stdin.buffer.fileno())
        self._control_reader = reader
        try:
            while True:
                chunk = await reader.read()
                if not chunk:
                    frames.finish()
                    await self._events.put(_ControlEOF())
                    return
                frames.feed(chunk)
                while (frame := frames.pop_frame()) is not None:
                    await self._events.put(_ControlFrame(frame))
        finally:
            reader.close()

    async def _observe_output(self, child: SupervisedChild, name: str, stream: IO[bytes]) -> None:
        reader = _AsyncInput(stream.fileno())
        child.readers[name] = reader
        try:
            while True:
                chunk = await reader.read()
                await self._events.put(_ChildOutput(child, name, chunk))
                if not chunk:
                    return
        finally:
            reader.close()

    async def _observe_exit(self, child: SupervisedChild) -> None:
        returncode = await child.wait_for_exit()
        await self._events.put(_ChildExited(child, returncode))

    async def _observe_deadline(
        self, child: SupervisedChild, timeout: float, *, readiness: bool
    ) -> None:
        await asyncio.sleep(timeout)
        await self._events.put(_Deadline(child, readiness))

    async def _guard(
        self,
        source: str,
        observation: Coroutine[Any, Any, None],
        child: SupervisedChild | None = None,
    ) -> _ObservationFailed | None:
        try:
            await observation
        except Exception as exc:
            failure = _ObservationFailed(source, exc, child)
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                # Cancellation must not block cleanup on a full event queue.
                return failure
            await self._events.put(failure)
        return None

    def _observe(
        self,
        source: str,
        observation: Coroutine[Any, Any, None],
        child: SupervisedChild | None = None,
    ) -> asyncio.Task[_ObservationFailed | None]:
        assert self._tasks is not None
        task = self._tasks.create_task(self._guard(source, observation, child), name=source)
        # A task cancelled before its first turn never enters _guard.
        task.add_done_callback(lambda _task: observation.close())
        if child is None:
            self._session_tasks.append(task)
        else:
            child.tasks.append(task)
        return task

    def _start_attempt(self) -> None:
        assert self.request is not None
        request = self.request
        if self._address_lease is not None:
            try:
                self._address_lease.close()
            except Exception as exc:
                self.cleanup_errors.append(exc)
            self._address_lease = None
        ports = [service.remote_port for service in request.services]
        if ports:
            allocated = allocate_service_address(ports)
            self.address = str(allocated)
            self._address_lease = getattr(allocated, "lease", None)
        else:
            self.address = random_address()
        argv = materialize_argv(
            request.argv,
            workspace=str(self.work),
            address=self.address,
            literal_prefix=request.literal_prefix,
        )
        replacements = {"{workspace}": str(self.work), "{address}": self.address}
        _check_required_paths(request.required_paths, replacements)
        emit("PROCESS_STARTING", argv=list(argv))
        self.child = _spawn_child(
            argv,
            cwd=self.work / "staged",
            environment=_child_environment(request),
            required_output_sentinels=request.required_output_sentinels,
        )
        self.state = _State.STARTING
        self._group_cleaned = False
        self._startup_exit = None
        child = self.child
        for name in ("stdout", "stderr"):
            stream = getattr(child.process, name)
            if stream is None:
                raise RuntimeError("child output was not captured")
            self._observe(name, self._observe_output(child, name, stream), child)
        self._observe("child exit", self._observe_exit(child), child)
        self._deadline_task = self._observe(
            "readiness deadline",
            self._observe_deadline(child, request.readiness_timeout, readiness=True),
            child,
        )
        if not request.required_output_sentinels:
            self._ready()

    def _ready(self) -> None:
        assert self.child is not None
        if self._pending_signum is not None:
            return
        self.state = _State.ACTIVE
        if self._deadline_task is not None:
            self._deadline_task.cancel()
        emit("PROCESS_READY", remote_address=self.address, child_pid=self.child.pid)

    def _select_failure(self, exception: BaseException, *, protocol: bool = False) -> None:
        if self.ending:
            return
        if protocol:
            self.protocol_error = exception
        else:
            self.operation_error = exception
        self.ending = True

    def _command(self, frame: bytes) -> None:
        if self.ending:
            return
        try:
            request = decode_command(json.loads(frame))
            if isinstance(request, StartRequest):
                if self.request is not None:
                    raise ValueError("START is only valid once")
                self.request = request
                self._start_attempt()
            else:
                self.ending = True
                self.close_reason = "requested"
        except Exception as exc:
            self._select_failure(exc, protocol=True)

    def _output(self, observation: _ChildOutput) -> None:
        child = observation.child
        if child is not self.child:
            return
        for fragment in child.decoders[observation.stream].feed(observation.chunk):
            child.startup_output.append(fragment)
            del child.startup_output[:-MAX_CAPTURED_STARTUP_FRAGMENTS]
            if self._output_available:
                try:
                    emit(
                        "CHILD_OUTPUT",
                        stream=fragment.stream,
                        payload=fragment.payload,
                        line_end=fragment.line_end,
                    )
                except Exception as exc:
                    self._output_available = False
                    if self.ending:
                        self.cleanup_errors.append(exc)
                    else:
                        self._select_failure(exc)
        if not observation.chunk:
            child.output_finished.add(observation.stream)
        if (
            self.state == _State.STARTING
            and not self.ending
            and child.required_output_sentinels.ready
        ):
            returncode = child.poll()
            if returncode is None:
                self._ready()
            else:
                self._exited(_ChildExited(child, returncode))

    def _exited(self, observation: _ChildExited) -> None:
        if observation.child is not self.child or self.ending:
            return
        if self.state == _State.STARTING:
            self._startup_exit = observation.returncode
            self._begin_child_cleanup()
        elif self.state == _State.ACTIVE:
            self.natural_returncode = observation.returncode
            self.close_reason = "process_exit"
            self.ending = True

    async def _observe_final_readiness(self, child: SupervisedChild) -> None:
        readers = list(child.readers.values())
        if self._control_reader is not None:
            readers.append(self._control_reader)
        # Readers acknowledge one raw scan, even when the fd has no new bytes.
        # Continue consuming the bounded queue while they publish those facts.
        if readers:
            await asyncio.wait([reader.checkpoint() for reader in readers])
        await self._events.put(_Deadline(child, readiness=True, final=True))

    def _final_readiness_observation(self, child: SupervisedChild) -> None:
        if child is self.child and self.state == _State.STARTING:
            returncode = child.poll()
            if returncode is not None:
                self._exited(_ChildExited(child, returncode))
            elif child.required_output_sentinels.ready:
                self._ready()
            else:
                self._select_failure(RuntimeError("process readiness timed out"), protocol=True)

    def _observation_failed(self, observation: _ObservationFailed) -> None:
        if observation.child is not None and observation.child is not self.child:
            return
        if observation.child is not None and observation.source in ("stdout", "stderr"):
            observation.child.output_finished.add(observation.source)
        if self.state == _State.TERMINATING and self.ending:
            self.cleanup_errors.append(observation.exception)
        else:
            self._select_failure(observation.exception, protocol=observation.source == "control")

    def _group_cleanup_finished(self, observation: _GroupCleaned) -> None:
        if observation.child is not self.child:
            return
        self._group_cleaned = True
        if observation.exception is not None:
            self.cleanup_errors.append(observation.exception)
        self._deadline_task = self._observe(
            "output drain deadline",
            self._observe_deadline(observation.child, CHILD_RELAY_JOIN_TIMEOUT, readiness=False),
            observation.child,
        )

    def _deadline(self, observation: _Deadline) -> None:
        if observation.child is not self.child:
            return
        if observation.readiness and self.state == _State.STARTING and not self.ending:
            if observation.final:
                self._final_readiness_observation(observation.child)
            else:
                self._observe(
                    "final readiness observation",
                    self._observe_final_readiness(observation.child),
                    observation.child,
                )
        elif (
            not observation.readiness
            and self.state == _State.TERMINATING
            and observation.child.output_finished != {"stdout", "stderr"}
        ):
            self.cleanup_errors.append(RuntimeError("child output relay did not stop"))
            observation.child.output_finished.update(("stdout", "stderr"))

    def _handle(self, observation: _Observation) -> None:
        if isinstance(observation, _ControlFrame):
            self._command(observation.frame)
        elif isinstance(observation, (_ControlEOF, _SignalReceived)):
            self.ending = True
        elif isinstance(observation, _ChildOutput):
            self._output(observation)
        elif isinstance(observation, _ChildExited):
            self._exited(observation)
        elif isinstance(observation, _ObservationFailed):
            self._observation_failed(observation)
        elif isinstance(observation, _GroupCleaned):
            self._group_cleanup_finished(observation)
        elif isinstance(observation, _Deadline):
            self._deadline(observation)

    async def _clean_group(self, child: SupervisedChild) -> None:
        failure = None
        try:
            await child.terminate()
        except Exception as exc:
            failure = exc
        await self._events.put(_GroupCleaned(child, failure))

    def _begin_child_cleanup(self) -> None:
        if self.state == _State.TERMINATING:
            return
        assert self.child is not None
        self.state = _State.TERMINATING
        if self._deadline_task is not None:
            self._deadline_task.cancel()
        self._observe("process-group cleanup", self._clean_group(self.child), self.child)

    async def _cancel(self, tasks: list[asyncio.Task[_ObservationFailed | None]]) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                failure = await task
                if failure is not None:
                    self.cleanup_errors.append(failure.exception)

    async def _finish_attempt(self) -> None:
        assert self.child is not None
        child = self.child
        await self._cancel(child.tasks)
        try:
            child.close_streams()
        except Exception as exc:
            self.cleanup_errors.append(exc)
        self.child = None
        if not self.ending and not self.cleanup_errors:
            if (
                is_bind_collision(child.startup_output)
                and self.attempt + 1 < MAX_ADDRESS_ALLOCATION_ATTEMPTS
            ):
                self.attempt += 1
                try:
                    self._start_attempt()
                except Exception as exc:
                    self._select_failure(exc, protocol=True)
                return
            self._select_failure(
                RuntimeError(f"process exited before readiness with status {self._startup_exit}"),
                protocol=True,
            )
        self.ending = True

    def _release_workspace(self) -> None:
        if self._resources_released:
            return
        self._resources_released = True
        if self._address_lease is not None:
            try:
                self._address_lease.close()
            except Exception as exc:
                self.cleanup_errors.append(exc)
            self._address_lease = None
        try:
            remove_workspace(self.work)
        except FileNotFoundError as exc:
            if self.work.exists():
                self.cleanup_errors.append(exc)
        except Exception as exc:
            self.cleanup_errors.append(exc)
        try:
            self.workspace_lock.close()
        except Exception as exc:
            self.cleanup_errors.append(exc)

    async def _coordinate(self) -> None:
        while self.state != _State.CLOSED:
            if self.ending:
                if self.child is None:
                    self.state = _State.CLOSED
                    break
                self._begin_child_cleanup()
            if (
                self.state == _State.TERMINATING
                and self._group_cleaned
                and self.child is not None
                and self.child.output_finished == {"stdout", "stderr"}
            ):
                await self._finish_attempt()
                continue
            self._handle(await self._events.get())
            # Let owned observers and the nonblocking protocol writer progress.
            await asyncio.sleep(0)

    async def run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        previous_handlers = {}
        output = None
        output_token = None
        final_exception: BaseException | None = None
        writer_task = None
        try:
            output = _ProtocolOutput()
            output_token = _protocol_output.set(output)
            async with asyncio.TaskGroup() as tasks:
                self._tasks = tasks
                try:
                    if output.descriptor is not None:
                        writer_task = self._observe("protocol output", output.run())
                    self.announce()
                    for signum in (signal.SIGTERM, signal.SIGINT):
                        previous_handlers[signum] = signal.getsignal(signum)
                        signal.signal(signum, self.handle_signal)
                    self._observe("control", self._observe_control())
                    self._observe("signal", self._observe_signals())
                    await self._coordinate()
                except BaseException as exc:
                    self._select_failure(exc)
                    await self._coordinate()
                finally:
                    await self._cancel(
                        [task for task in self._session_tasks if task is not writer_task]
                    )
                    self._release_workspace()
                try:
                    self._report_outcome()
                except BaseException as exc:
                    final_exception = exc
                    if isinstance(exc, Exception):
                        try:
                            error(exc)
                        except Exception as output_error:
                            exc.add_note(f"terminal output also failed: {output_error}")
                try:
                    await output.drain()
                except Exception as exc:
                    if final_exception is None:
                        final_exception = exc
                    else:
                        final_exception.add_note(f"protocol output cleanup also failed: {exc}")
                finally:
                    if writer_task is not None:
                        await self._cancel([writer_task])
        except BaseException as exc:
            final_exception = exc
        finally:
            self._tasks = None
            self._loop = None
            self._release_workspace()
            if output_token is not None:
                _protocol_output.reset(output_token)
            if output is not None:
                try:
                    output.close()
                except Exception as exc:
                    if final_exception is None:
                        final_exception = exc
                    else:
                        final_exception.add_note(f"protocol output cleanup also failed: {exc}")
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except Exception as exc:
                    self.cleanup_errors.append(exc)
                    if final_exception is None:
                        final_exception = exc
            if final_exception is not None:
                for cleanup_error in self.cleanup_errors:
                    if cleanup_error is not final_exception:
                        note = f"session cleanup also failed: {cleanup_error}"
                        if note not in getattr(final_exception, "__notes__", ()):
                            final_exception.add_note(note)
        if final_exception is not None:
            raise final_exception

    def _report_outcome(self) -> None:
        failure = self.protocol_error or self.operation_error
        if failure is not None:
            for exc in self.cleanup_errors:
                failure.add_note(f"session cleanup also failed: {exc}")
            if self.protocol_error is not None:
                error(failure, "PROTOCOL_ERROR")
                if self.cleanup_errors:
                    raise SystemExit(1)
                return
            raise failure
        _raise_cleanup_errors(self.cleanup_errors)
        if self.close_reason is not None:
            emit("SESSION_CLOSED", reason=self.close_reason, returncode=self.natural_returncode)

    def run(self) -> None:
        asyncio.run(self.run_async())


def control():
    session = ControlSession.create()
    try:
        session.run()
    except Exception as exc:
        # The session owns ERROR delivery and bounded output cleanup; never
        # retry a blocking stdout write after the structured scope has closed.
        raise SystemExit(1) from exc


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("control")
    staging = sub.add_parser("stage")
    staging.add_argument("workspace")
    version = sub.add_parser("openocd-version")
    version.add_argument("executable", nargs="+")
    args = parser.parse_args()
    if args.command == "control":
        control()
    elif args.command == "stage":
        stage(args.workspace)
    elif args.command == "openocd-version":
        openocd_version(args.executable)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error(exc)
        raise SystemExit(1) from exc
