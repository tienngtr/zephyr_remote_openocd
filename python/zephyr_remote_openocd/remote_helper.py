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
from collections.abc import Callable, Coroutine, Iterable, Iterator
from contextlib import contextmanager, suppress
from enum import Enum, auto
from itertools import chain
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import IO, Any, Literal, NamedTuple

VERSION = 1
RANGE = ipaddress.IPv4Network("127.64.0.0/10")
SESSION_LOCK = ".session.lock"
UNCONFIRMED_CHILD = ".child-disposal-unconfirmed"
STALE_SESSION_AGE = 24 * 60 * 60
WORKSPACE_LEASE_TIMEOUT = 5
WORKSPACE_LEASE_POLL_INTERVAL = 0.05
CHILD_TERM_TIMEOUT = 5
CHILD_POLL_INTERVAL = 0.05
CHILD_REAP_TIMEOUT = 1
CHILD_GROUP_EXIT_TIMEOUT = 1
CHILD_RELAY_JOIN_TIMEOUT = 2
RELAY_CHUNK_SIZE = 64 * 1024
# Keep in sync with remote/protocol.py; this file is deployed standalone.
MAX_CONTROL_FRAME_SIZE = 1024 * 1024
# A startup cut can inspect one maximum control frame's worth per raw source.
# Continuously arriving bytes cannot keep the final scan open indefinitely.
MAX_FINAL_OBSERVATION_BYTES = MAX_CONTROL_FRAME_SIZE
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
        for note in tuple(getattr(error, "__notes__", ())):
            first.add_note(f"additional cleanup failure detail: {note}")
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
    diagnostic = "\n".join((str(message), *getattr(message, "__notes__", ())))
    emit("ERROR", code=code, message=diagnostic)


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
    # Allocation creates the lease. A delayed upload must not recreate metadata
    # after successful cleanup has removed the workspace and its admission state.
    with _lease_path(work).open("r+b") as lease:
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
    # Keep the lease identity and closed admission until workspace removal
    # succeeds; delayed stages reject missing workspaces even on an old inode.
    close_staging_admission(work)
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
        errors = []
        for metadata in (_closure_path(work), _lease_path(work)):
            try:
                metadata.unlink(missing_ok=True)
            except BaseException as exc:
                errors.append(exc)
        _raise_cleanup_errors(errors)


def close_staging_admission(work: Path) -> None:
    """Publish closure independently of child cleanup or an active upload."""
    with _closure_path(work).open("ab"):
        pass


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
            if (path / UNCONFIRMED_CHILD).exists():
                # Helper exit cannot prove that a residual child stopped using
                # these inputs. Age and an unlocked session are insufficient.
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
            except OSError:
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
            with _lease_path(path).open("xb"):
                pass
            return session_id, path, lock
        except BaseException as failure:
            cleanups = [lambda work=path: remove_workspace(work)]
            if lock is not None:
                cleanups.append(lock.close)
            for cleanup in cleanups:
                try:
                    cleanup()
                except BaseException as cleanup_error:
                    failure.add_note(f"workspace allocation rollback also failed: {cleanup_error}")
                    for note in getattr(cleanup_error, "__notes__", ()):
                        failure.add_note(f"workspace allocation rollback detail: {note}")
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


class _ValidatedArchiveMember(NamedTuple):
    member: tarfile.TarInfo
    relative: PurePosixPath
    target: Path
    kind: Literal["directory", "file"]


class _StagingResult(NamedTuple):
    byte_count: int
    sha256: str
    files: tuple[str, ...]
    directories: tuple[str, ...]


def _validate_archive(
    archive: tarfile.TarFile, target_root: Path
) -> tuple[_ValidatedArchiveMember, ...]:
    """Validate the complete archive before any extraction can mutate its targets."""
    members = archive.getmembers()
    seen: set[PurePosixPath] = set()
    validated = []
    kinds = {}
    for member in members:
        relative = valid_member(member, seen)
        kind: Literal["directory", "file"] = "directory" if member.isdir() else "file"
        kinds[relative] = kind
        target = target_root.joinpath(*relative.parts)
        if target_root.resolve() not in target.resolve().parents:
            raise ValueError(f"archive path escapes staging directory: {relative}")
        validated.append(_ValidatedArchiveMember(member, relative, target, kind))
    if any(
        kind == "file" and any(path in other.parents for other in kinds)
        for path, kind in kinds.items()
    ):
        raise ValueError("archive contains a file/directory ancestor conflict")
    return tuple(validated)


def _extract_archive(
    archive: tarfile.TarFile, validated: tuple[_ValidatedArchiveMember, ...]
) -> _StagingResult:
    """Extract an already-validated manifest and summarize regular-file content."""
    count = 0
    digest = hashlib.sha256()
    names = []
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
    return _StagingResult(
        count,
        digest.hexdigest(),
        tuple(names),
        tuple(str(relative) for _, relative, _, kind in validated if kind == "directory"),
    )


def stage(workspace: str | os.PathLike[str]) -> None:
    root = workspace_root().resolve()
    work = Path(workspace).resolve()
    if root not in work.parents or work.parent != root or not work.is_dir():
        raise ValueError("workspace is not an active helper session")
    target_root = work / "staged"
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")  # noqa: SIM115
    try:
        shutil.copyfileobj(sys.stdin.buffer, spool, length=1024 * 1024)
        with _stage_lease(work):
            spool.seek(0)
            with tarfile.open(fileobj=spool, mode="r:*") as archive:
                validated = _validate_archive(archive, target_root)
                result = _extract_archive(archive, validated)
            emit(
                "STAGED",
                byte_count=result.byte_count,
                sha256=result.sha256,
                files=list(result.files),
                directories=list(result.directories),
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


def allocate_service_address(ports, preferred_address=None):
    preferred = () if preferred_address is None else (preferred_address,)
    candidates = chain(
        preferred, (random_address() for _ in range(MAX_ADDRESS_ALLOCATION_ATTEMPTS))
    )
    for address in candidates:
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


class SessionValue(NamedTuple):
    name: str


class TclWord(NamedTuple):
    parts: tuple[str | SessionValue, ...]


class ArgumentTemplate(NamedTuple):
    parts: tuple[str | SessionValue | TclWord, ...]


def _parse_template_parts(values, *, allow_tcl=True):
    if not isinstance(values, list) or not values:
        raise ValueError("invalid argument template parts")
    parts: list[str | SessionValue | TclWord] = []
    for value in values:
        if isinstance(value, str) and "\0" not in value:
            parts.append(value)
        elif isinstance(value, dict) and set(value) == {"session"}:
            if value["session"] not in ("workspace", "address"):
                raise ValueError("invalid argument template session value")
            parts.append(SessionValue(value["session"]))
        elif allow_tcl and isinstance(value, dict) and set(value) == {"tcl_word"}:
            parts.append(TclWord(_parse_template_parts(value["tcl_word"], allow_tcl=False)))
        else:
            raise ValueError("invalid argument template part")
    return tuple(parts)


def _parse_argv_templates(values, argv_length, literal_prefix):
    if not isinstance(values, list):
        raise ValueError("invalid argv templates")
    templates = []
    seen = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {"index", "parts"}:
            raise ValueError("invalid argv template")
        index = value["index"]
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not literal_prefix <= index < argv_length
            or index in seen
        ):
            raise ValueError("invalid argv template index")
        seen.add(index)
        templates.append((index, ArgumentTemplate(_parse_template_parts(value["parts"]))))
    return tuple(templates)


def _tcl_quote(value):
    # Keep the literal-word quoting rule in sync with remote/tcl.py. The helper
    # is deployed standalone and cannot import the local package.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for character in "$[]{}":
        escaped = escaped.replace(character, "\\" + character)
    return '"' + escaped + '"'


def _render_parts(parts, replacements):
    result = []
    for part in parts:
        if isinstance(part, str):
            result.append(part)
        elif isinstance(part, SessionValue):
            result.append(replacements[part.name])
        else:
            result.append(_tcl_quote(_render_parts(part.parts, replacements)))
    return "".join(result)


def materialize_path(value, *, workspace, address):
    if isinstance(value, str):
        return value
    return _render_parts(value.parts, {"workspace": workspace, "address": address})


def _check_required_paths(checks, replacements):
    for check in checks:
        path = materialize_path(check.path, **replacements)
        if not path:
            raise ValueError("required remote path must not be empty")
        candidate = Path(path)
        valid = candidate.is_file() if check.kind == "file" else candidate.is_dir()
        if not valid:
            raise ValueError(f"required remote {check.kind} is missing: {candidate}")


class ServiceRequest(NamedTuple):
    """Validated service data from one START request."""

    name: str
    remote_port: int


class RequiredPath(NamedTuple):
    kind: str
    path: str | ArgumentTemplate


class StartRequest(NamedTuple):
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    required_paths: tuple[RequiredPath, ...]
    services: tuple[ServiceRequest, ...]
    required_output_sentinels: tuple[str, ...]
    readiness_timeout: float
    literal_prefix: int
    argv_templates: tuple[tuple[int, ArgumentTemplate], ...]
    preferred_address: str | None


def _validate_preferred_address(address):
    if address is None:
        return
    if not isinstance(address, str):
        raise ValueError("preferred address must be a string or null")
    parsed = ipaddress.IPv4Address(address)
    if (
        str(parsed) != address
        or parsed not in RANGE
        or parsed in (RANGE.network_address, RANGE.broadcast_address)
    ):
        raise ValueError("preferred address must be a usable address in 127.64.0.0/10")


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
    ):
        raise ValueError("invalid required-path assertion")
    path = item["path"]
    if isinstance(path, str):
        if not path or "\0" in path:
            raise ValueError("invalid required-path assertion")
    elif isinstance(path, dict) and set(path) == {"parts"}:
        path = ArgumentTemplate(_parse_template_parts(path["parts"], allow_tcl=False))
    else:
        raise ValueError("invalid required-path assertion")
    return RequiredPath(item["kind"], path)


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
        "argv_templates",
        "preferred_address",
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
    templates = _parse_argv_templates(message["argv_templates"], len(argv), literal_prefix)
    preferred_address = message["preferred_address"]
    _validate_preferred_address(preferred_address)
    return StartRequest(
        tuple(argv),
        tuple(environment.items()),
        checks,
        services,
        tuple(sentinels),
        float(timeout),
        literal_prefix,
        templates,
        preferred_address,
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


def _acquire_nonblocking(descriptor: int) -> bool:
    """Own mode rollback until the reader/writer can adopt the descriptor."""
    previous = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
    except BaseException as failure:
        try:
            os.set_blocking(descriptor, previous)
        except BaseException as cleanup_error:
            failure.add_note(f"descriptor mode rollback also failed: {cleanup_error}")
            for note in getattr(cleanup_error, "__notes__", ()):
                failure.add_note(f"descriptor mode rollback detail: {note}")
        raise
    return previous


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
            self.was_blocking = _acquire_nonblocking(self.descriptor)

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
        try:
            if self.descriptor is not None and self.was_blocking is not None:
                os.set_blocking(self.descriptor, self.was_blocking)
        finally:
            self.frames.clear()
            self.pending_bytes = 0
            self.offset = 0
            self.empty.set()


class _AsyncInput:
    """One cancellable raw observation path for an owned input descriptor."""

    def __init__(
        self,
        descriptor: int,
        *,
        on_idle: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ):
        self.descriptor = descriptor
        self._on_idle = on_idle
        self._wake: asyncio.Future[None] | None = None
        self._checkpoint: asyncio.Future[None] | None = None
        self._scan_remaining = 0
        self._closed = False
        self.was_blocking = _acquire_nonblocking(descriptor)

    def _observed(self) -> None:
        if self._checkpoint is not None and not self._checkpoint.done():
            self._scan_remaining = 0
            self._checkpoint.set_result(None)

    def _wake_reader(self) -> None:
        if self._wake is not None and not self._wake.done():
            self._wake.set_result(None)

    def wake(self) -> None:
        """Let the sole observer make progress without another descriptor reader."""
        self._wake_reader()

    def checkpoint(self) -> asyncio.Future[None]:
        """Request a finite available-byte scan by the sole descriptor reader."""
        self._checkpoint = asyncio.get_running_loop().create_future()
        self._scan_remaining = MAX_FINAL_OBSERVATION_BYTES
        if self._closed:
            self._observed()
        else:
            self._wake_reader()
        return self._checkpoint

    async def read(self) -> bytes:
        while True:
            try:
                size = min(RELAY_CHUNK_SIZE, self._scan_remaining or RELAY_CHUNK_SIZE)
                chunk = os.read(self.descriptor, size)
            except BlockingIOError:
                self._observed()
                if self._on_idle is not None:
                    await self._on_idle()
                loop = asyncio.get_running_loop()
                self._wake = loop.create_future()
                loop.add_reader(self.descriptor, self._wake_reader)
                try:
                    await self._wake
                finally:
                    loop.remove_reader(self.descriptor)
                    self._wake = None
            else:
                if not chunk:
                    self._observed()
                elif self._scan_remaining:
                    self._scan_remaining -= len(chunk)
                    if not self._scan_remaining:
                        self._observed()
                return chunk

    def close(self) -> None:
        self._closed = True
        self._observed()
        os.set_blocking(self.descriptor, self.was_blocking)


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _group_exit_waits(pid: int) -> Iterator[float]:
    """Poll group disappearance after reaping, without signalling or enumerating."""
    deadline = time.monotonic() + CHILD_GROUP_EXIT_TIMEOUT
    while time.monotonic() < deadline:
        if not _group_exists(pid):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        yield min(CHILD_POLL_INTERVAL, remaining)
    raise TimeoutError(f"process group {pid} did not disappear during cleanup")


class _ChildAcquisition:
    """Own spawn results before their adoption by the coordinator."""

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.process: subprocess.Popen[bytes] | None = None
        self.child: SupervisedChild | None = None
        self.producer_quiescent = False
        self.rollback_confirmed = False


class _ChildSettlement(NamedTuple):
    generation: int
    producer_quiescent: bool
    group_disposed: bool
    observers_settled: bool
    pipes_closed: bool

    @property
    def confirmed(self) -> bool:
        return (
            self.producer_quiescent
            and self.group_disposed
            and self.observers_settled
            and self.pipes_closed
        )


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
        self.group_disposed = False
        self.termination_requested = False

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
        if self.group_disposed:
            return
        errors = []
        if self.process.returncode is None:
            # Capture already observable natural status before signalling. Group
            # disposal still applies when descendants outlive that leader.
            try:
                observed = self.poll()
            except BaseException as exc:
                errors.append(exc)
                observed = None
            group_exists = True
            try:
                os.killpg(self.pid, signal.SIGTERM)
                if observed is None:
                    self.termination_requested = True
            except ProcessLookupError:
                group_exists = False
            except BaseException as exc:
                errors.append(exc)
            if group_exists:
                try:
                    await self._wait_for_leader_exit()
                except BaseException as exc:
                    errors.append(exc)
                try:
                    group_exists = _group_exists(self.pid)
                except BaseException as exc:
                    errors.append(exc)
                    group_exists = True
                if group_exists:
                    with suppress(BaseException):
                        self._warn_remaining_group_members()
                    try:
                        observed = self.poll()
                    except BaseException as exc:
                        errors.append(exc)
                        observed = None
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                        if observed is None:
                            self.termination_requested = True
                    except ProcessLookupError:
                        pass
                    except BaseException as exc:
                        errors.append(exc)
            try:
                # Reap only after group signalling; this wait has a finite budget.
                self._observed_returncode = self.process.wait(timeout=CHILD_REAP_TIMEOUT)
            except BaseException as exc:
                errors.append(exc)
        try:
            # An unreaped leader itself keeps the group observable. Once
            # reaped, only observe: its PID is no longer reserved for us.
            for delay in _group_exit_waits(self.pid):
                await asyncio.sleep(delay)
            self.group_disposed = True
        except BaseException as exc:
            errors.append(exc)
        _raise_cleanup_errors(errors)

    def close_streams(self) -> None:
        errors = []
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except BaseException as exc:
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
    try:
        for delay in _group_exit_waits(process.pid):
            time.sleep(delay)
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


def _spawn_child(
    argv,
    *,
    cwd=None,
    environment=None,
    required_output_sentinels=(),
    ownership: _ChildAcquisition | None = None,
) -> SupervisedChild:
    owner = ownership if ownership is not None else _ChildAcquisition(1)
    try:
        owner.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        owner.child = SupervisedChild(owner.process, required_output_sentinels)
        return owner.child
    except BaseException as error:
        if owner.process is not None:
            try:
                _rollback_spawned_process(owner.process)
                owner.rollback_confirmed = True
            except BaseException as cleanup_error:
                error.add_note(f"child ownership rollback also failed: {cleanup_error}")
                for note in tuple(getattr(cleanup_error, "__notes__", ())):
                    error.add_note(f"child ownership rollback detail: {note}")
        raise
    finally:
        owner.producer_quiescent = True


def materialize_argv(
    argv: Iterable[str],
    *,
    workspace: str,
    address: str,
    literal_prefix: int = 0,
    argv_templates: Iterable[tuple[int, ArgumentTemplate]] = (),
) -> tuple[str, ...]:
    """Resolve only explicit runner-owned templates, preserving all literal text.

    The helper reports this exact argv before spawning each child attempt so
    diagnostics never reconstruct session expansion independently.
    """
    replacements = {"workspace": workspace, "address": address}
    templates = dict(argv_templates)
    return tuple(
        _render_parts(templates[index].parts, replacements)
        if index >= literal_prefix and index in templates
        else arg
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
    exception: BaseException | None


class _ControlFence(NamedTuple):
    observed: asyncio.Future[None]
    resume: asyncio.Future[None]


_Observation = (
    _ControlFrame
    | _ControlEOF
    | _ChildOutput
    | _ChildExited
    | _Deadline
    | _SignalReceived
    | _ObservationFailed
    | _GroupCleaned
    | _ControlFence
)


class _SessionSignals:
    """Own installed session handlers, including partially completed setup."""

    def __init__(self) -> None:
        self.previous_handlers: dict[
            int, Callable[[int, FrameType | None], object] | int | None
        ] = {}
        self.pending_signum: int | None = None

    def capture(self, signum: int) -> bool:
        """Latch only a native fact, without mutating asyncio or lifecycle state."""
        if self.pending_signum is not None:
            return False
        self.pending_signum = signum
        return True

    def install(self, handler: Callable[[int, FrameType | None], None]) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)

    def restore(self) -> Iterator[BaseException]:
        """Report each failed restoration before attempting the next handler."""
        for signum, handler in self.previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as exc:
                yield exc


class ControlSession:
    """Sole lifecycle owner of one structured remote-helper session."""

    def __init__(self, session_id: str, work: Path, workspace_lock: IO[bytes]) -> None:
        self.session_id = session_id
        self.work = work
        self.workspace_lock = workspace_lock
        self.child: SupervisedChild | None = None
        self.protocol_error: BaseException | None = None
        self._child_acquisition: _ChildAcquisition | None = None
        self._child_settlement: _ChildSettlement | None = None
        self._staging_closed = False
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
        self._signal_scope = _SessionSignals()
        self._tasks: asyncio.TaskGroup | None = None
        self._session_tasks: list[asyncio.Task[_ObservationFailed | None]] = []
        self._observation_failures: dict[asyncio.Task[Any], _ObservationFailed] = {}
        self._resources_released = False
        self._output_available = True
        self._control_reader: _AsyncInput | None = None
        self._control_task: asyncio.Task[_ObservationFailed | None] | None = None
        self._control_fence: _ControlFence | None = None
        self._pending_control: _ControlFrame | _ControlEOF | None = None
        self._reconciling_readiness = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def create(cls) -> ControlSession:
        session_id, work, workspace_lock = new_workspace()
        try:
            return cls(session_id, work, workspace_lock)
        except BaseException as failure:
            # Allocation owns these resources until construction succeeds.
            # A failed directory removal must not prevent lock release.
            for cleanup in (lambda: remove_workspace(work), workspace_lock.close):
                try:
                    cleanup()
                except BaseException as exc:
                    failure.add_note(f"workspace adoption cleanup also failed: {exc}")
                    for note in getattr(exc, "__notes__", ()):
                        failure.add_note(f"workspace adoption cleanup detail: {note}")
            raise

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
        if not self._signal_scope.capture(signum):
            return
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
        reader = _AsyncInput(sys.stdin.buffer.fileno(), on_idle=self._acknowledge_control_fence)
        self._control_reader = reader
        try:
            while True:
                chunk = await reader.read()
                if not chunk:
                    frames.finish()
                    self._pending_control = _ControlEOF()
                    await self._events.put(self._pending_control)
                    self._pending_control = None
                    return
                frames.feed(chunk)
                while (frame := frames.pop_frame()) is not None:
                    self._pending_control = _ControlFrame(frame)
                    await self._events.put(self._pending_control)
                    self._pending_control = None
                await self._acknowledge_control_fence()
        finally:
            reader.close()

    async def _acknowledge_control_fence(self) -> None:
        fence = self._control_fence
        if fence is not None:
            # Called only after publishing a whole consumed batch or scanning
            # an idle descriptor. Pause observation until the coordinator has
            # dispatched the fence and made its retry decision.
            fence.observed.set_result(None)
            await fence.resume

    async def _observe_control_fence(self, fence: _ControlFence) -> None:
        if self._control_task is not None:
            # A completed guard has published EOF or its framing/read failure.
            # Unlike a raw-reader checkpoint, this also covers queue backpressure.
            await asyncio.wait(
                (fence.observed, self._control_task), return_when=asyncio.FIRST_COMPLETED
            )
        await self._events.put(fence)

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
            if task is not None:
                # Recognition precedes publication, which may block or be
                # cancelled before the coordinator can dispatch the failure.
                self._observation_failures[task] = failure
                if task.cancelling():
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
            # Failed retirement leaves the old owner reachable and prevents a
            # replacement spawn; terminal cleanup still attempts its release.
            self._address_lease.close()
            self._address_lease = None
        ports = [service.remote_port for service in request.services]
        allocated = allocate_service_address(
            ports, preferred_address=request.preferred_address if self.attempt == 0 else None
        )
        self._address_lease = getattr(allocated, "lease", None)
        self.address = str(allocated)
        argv = materialize_argv(
            request.argv,
            workspace=str(self.work),
            address=self.address,
            literal_prefix=request.literal_prefix,
            argv_templates=request.argv_templates,
        )
        _validate_argv(list(argv))
        replacements = {"workspace": str(self.work), "address": self.address}
        _check_required_paths(request.required_paths, replacements)
        emit("PROCESS_STARTING", argv=list(argv))
        # Publish conservative retention before any child can acquire inputs.
        # A later metadata write failure must not enable stale reclamation.
        (self.work / UNCONFIRMED_CHILD).touch(mode=0o600)
        owner = _ChildAcquisition(self.attempt + 1)
        self._child_acquisition = owner
        self._child_settlement = None
        try:
            child = _spawn_child(
                argv,
                cwd=self.work / "staged",
                environment=_child_environment(request),
                required_output_sentinels=request.required_output_sentinels,
                ownership=owner,
            )
            self.child = child
        finally:
            # The ticket stays reachable even if return/adoption is interrupted.
            owner.producer_quiescent = True
            if self.child is None:
                self.child = owner.child
        self.state = _State.STARTING
        self._group_cleaned = False
        self._startup_exit = None
        self._start_output_observers(child)
        self._observe("child exit", self._observe_exit(child), child)
        self._deadline_task = self._observe(
            "readiness deadline",
            self._observe_deadline(child, request.readiness_timeout, readiness=True),
            child,
        )
        if not request.required_output_sentinels:
            self._ready()

    def _start_output_observers(self, child: SupervisedChild) -> None:
        observed = {task.get_name() for task in child.tasks}
        for name in ("stdout", "stderr"):
            if name in observed:
                continue
            stream = getattr(child.process, name)
            if stream is None:
                raise RuntimeError("child output was not captured")
            if stream.closed:
                child.output_finished.add(name)
                continue
            self._observe(name, self._observe_output(child, name, stream), child)

    def _ready(self) -> None:
        assert self.child is not None
        if self._reconciling_readiness:
            return
        # Dispatch the bounded queue before success. Output dispatch may request
        # readiness again; the outer call alone owns this publication decision.
        self._reconciling_readiness = True
        try:
            while not self._events.empty():
                self._handle(self._events.get_nowait())
                if self.ending:
                    return
            # A control observer may have consumed a fact but be blocked at put.
            # It remains owned here until normal publication; terminal dispatch
            # makes any later duplicate harmless through the existing guards.
            if self._pending_control is not None:
                self._handle(self._pending_control)
                if self.ending:
                    return
        finally:
            self._reconciling_readiness = False
        # A guard may already know a fatal fact while its queue publication is
        # blocked. Reconcile through the coordinator's existing failure path.
        for failure in tuple(self._observation_failures.values()):
            self._observation_failed(failure)
            if self.ending:
                return
        if (
            self.ending
            or self.state != _State.STARTING
            or self._signal_scope.pending_signum is not None
        ):
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
        for task, failure in self._observation_failures.items():
            if failure is observation:
                del self._observation_failures[task]
                break
        else:
            # Joining the owner may already have accounted for this queued fact.
            return
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
        except BaseException as exc:
            failure = exc
        await self._events.put(_GroupCleaned(child, failure))

    def _begin_child_cleanup(self) -> None:
        if self.state == _State.TERMINATING:
            return
        assert self.child is not None
        self.state = _State.TERMINATING
        # Partial adoption still owns captured pipes. Give their sole readers
        # the same finite drain opportunity as normally initialized attempts.
        self._start_output_observers(self.child)
        if self._deadline_task is not None:
            self._deadline_task.cancel()
        self._observe("process-group cleanup", self._clean_group(self.child), self.child)

    async def _cancel(self, tasks: list[asyncio.Task[_ObservationFailed | None]]) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                self.cleanup_errors.append(exc)
            failure = self._observation_failures.pop(task, None)
            if failure is not None:
                self.cleanup_errors.append(failure.exception)

    async def _finish_attempt(self) -> None:
        assert self.child is not None
        child = self.child
        await self._cancel(child.tasks)
        try:
            child.close_streams()
        except BaseException as exc:
            self.cleanup_errors.append(exc)
        owner = self._child_acquisition
        assert owner is not None
        self._child_settlement = _ChildSettlement(
            owner.generation,
            owner.producer_quiescent,
            child.group_disposed,
            all(task.done() for task in child.tasks),
            all(
                stream is None or stream.closed
                for stream in (child.process.stdout, child.process.stderr)
            ),
        )
        retry = (
            not self.ending
            and not self.cleanup_errors
            and self._child_settlement.confirmed
            and is_bind_collision(child.startup_output)
            and self.attempt + 1 < MAX_ADDRESS_ALLOCATION_ATTEMPTS
        )
        if retry:
            await self._retry_commit_boundary()
        if self._child_settlement.confirmed:
            self.child = None
        else:
            self.cleanup_errors.append(RuntimeError("child disposal remains unconfirmed"))
            self.ending = True
            self.state = _State.CLOSED
        if not self.ending and not self.cleanup_errors:
            if retry:
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

    async def _retry_commit_boundary(self) -> None:
        loop = asyncio.get_running_loop()
        fence = _ControlFence(loop.create_future(), loop.create_future())
        self._control_fence = fence
        if self._control_reader is not None:
            self._control_reader.wake()
        self._observe("retry control fence", self._observe_control_fence(fence))
        try:
            while not self.ending and not self.cleanup_errors:
                observation = await self._events.get()
                if observation is fence:
                    break
                self._handle(observation)
                await asyncio.sleep(0)
            if self._signal_scope.pending_signum is not None:
                self.ending = True
        finally:
            self._control_fence = None
            if not fence.resume.done():
                fence.resume.set_result(None)

    def _release_workspace(self) -> None:
        if self._resources_released:
            return
        self._resources_released = True
        if self._address_lease is not None:
            try:
                self._address_lease.close()
            except BaseException as exc:
                self.cleanup_errors.append(exc)
            self._address_lease = None
        try:
            owner = self._child_acquisition
            disposed = (
                owner is None
                or owner.process is None
                or owner.rollback_confirmed
                or self._child_settlement is not None
                and self._child_settlement.confirmed
            )
            if disposed:
                try:
                    (self.work / UNCONFIRMED_CHILD).unlink(missing_ok=True)
                except BaseException as exc:
                    self.cleanup_errors.append(exc)
                remove_workspace(self.work)
            else:
                close_staging_admission(self.work)
                self.cleanup_errors.append(
                    RuntimeError("retaining workspace for unconfirmed child disposal")
                )
        except FileNotFoundError as exc:
            if self.work.exists():
                self.cleanup_errors.append(exc)
        except BaseException as exc:
            self.cleanup_errors.append(exc)
        try:
            self.workspace_lock.close()
        except BaseException as exc:
            self.cleanup_errors.append(exc)

    async def _coordinate(self) -> None:
        while self.state != _State.CLOSED:
            if self.ending:
                if not self._staging_closed:
                    try:
                        close_staging_admission(self.work)
                    except BaseException as exc:
                        self.cleanup_errors.append(exc)
                    self._staging_closed = True
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

    async def _run_session_tasks(
        self, output: _ProtocolOutput, signals: _SessionSignals
    ) -> BaseException | None:
        """Keep the writer owned until observers stop and terminal output drains."""
        final_exception: BaseException | None = None
        writer_task = None
        async with asyncio.TaskGroup() as tasks:
            self._tasks = tasks
            try:
                if output.descriptor is not None:
                    writer_task = self._observe("protocol output", output.run())
                self.announce()
                signals.install(self.handle_signal)
                self._control_task = self._observe("control", self._observe_control())
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
                        for note in tuple(getattr(output_error, "__notes__", ())):
                            exc.add_note(f"terminal output failure detail: {note}")
            try:
                await output.drain()
            except Exception as exc:
                if final_exception is None:
                    final_exception = exc
                else:
                    final_exception.add_note(f"protocol output cleanup also failed: {exc}")
                    for note in tuple(getattr(exc, "__notes__", ())):
                        final_exception.add_note(f"protocol output cleanup detail: {note}")
            finally:
                if writer_task is not None:
                    await self._cancel([writer_task])
        return final_exception

    async def run_async(self) -> None:
        """Own output and signal scopes around the structured session tasks."""
        self._loop = asyncio.get_running_loop()
        if self._signal_scope.pending_signum is not None:
            # Publish a pre-loop signal through the same observation path as
            # later signals, without overtaking already-published control facts.
            self._loop.call_soon(self._enqueue_signal, self._signal_scope.pending_signum)
        signals = self._signal_scope
        output = None
        output_token = None
        final_exception: BaseException | None = None
        try:
            output = _ProtocolOutput()
            output_token = _protocol_output.set(output)
            final_exception = await self._run_session_tasks(output, signals)
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
                except BaseException as exc:
                    if final_exception is None:
                        final_exception = exc
                    else:
                        final_exception.add_note(f"protocol output cleanup also failed: {exc}")
                        for note in tuple(getattr(exc, "__notes__", ())):
                            final_exception.add_note(f"protocol output cleanup detail: {note}")
            for restoration_error in signals.restore():
                self.cleanup_errors.append(restoration_error)
                if final_exception is None:
                    final_exception = restoration_error
            if final_exception is not None:
                for cleanup_error in self.cleanup_errors:
                    if cleanup_error is not final_exception:
                        note = f"session cleanup also failed: {cleanup_error}"
                        if note not in getattr(final_exception, "__notes__", ()):
                            final_exception.add_note(note)
                        for detail in tuple(getattr(cleanup_error, "__notes__", ())):
                            note = f"session cleanup failure detail: {detail}"
                            if note not in getattr(final_exception, "__notes__", ()):
                                final_exception.add_note(note)
        if final_exception is not None:
            raise final_exception

    def _report_outcome(self) -> None:
        failure = self.protocol_error or self.operation_error
        if failure is not None:
            for exc in self.cleanup_errors:
                failure.add_note(f"session cleanup also failed: {exc}")
                for note in tuple(getattr(exc, "__notes__", ())):
                    failure.add_note(f"session cleanup failure detail: {note}")
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
        # Set up the loop before creating its coroutine; failed loop setup
        # leaves only the workspace for the control entry point to release.
        with asyncio.Runner() as runner:
            runner.run(self.run_async())


def control() -> None:
    session: ControlSession | None = None
    pending_signum: int | None = None
    previous_handlers = {}
    failure: BaseException | None = None
    cleanup_errors: list[BaseException] = []

    def handle_signal(signum: int, frame: FrameType | None) -> None:
        nonlocal pending_signum
        if pending_signum is None:
            pending_signum = signum
        if session is not None:
            session.handle_signal(signum, frame)

    try:
        # Signals report facts throughout allocation and adoption. They cannot
        # interrupt rollback or leave a successfully allocated tuple unowned.
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        session = ControlSession.create()
        if pending_signum is not None:
            session.handle_signal(pending_signum)
        session.run()
    except BaseException as exc:
        failure = exc
    finally:
        if session is not None:
            # run_async normally releases these resources. This also covers
            # failure before its cleanup scope is entered (e.g. loop setup).
            session._release_workspace()
            cleanup_errors.extend(session.cleanup_errors)
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except BaseException as exc:
                cleanup_errors.append(exc)
    if failure is None and cleanup_errors:
        failure = cleanup_errors.pop(0)
    if failure is not None:
        for cleanup_error in cleanup_errors:
            if cleanup_error is not failure:
                note = f"session cleanup also failed: {cleanup_error}"
                if note not in getattr(failure, "__notes__", ()):
                    failure.add_note(note)
                for detail in tuple(getattr(cleanup_error, "__notes__", ())):
                    note = f"session cleanup failure detail: {detail}"
                    if note not in getattr(failure, "__notes__", ()):
                        failure.add_note(note)
        # The session owns ERROR delivery and bounded output cleanup; never
        # retry a blocking stdout write after the structured scope has closed.
        if session is not None and isinstance(failure, Exception):
            raise SystemExit(1) from failure
        raise failure


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
