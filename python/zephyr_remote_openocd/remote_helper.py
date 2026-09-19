#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Protocol v1 remote helper. This file is deliberately self-contained."""

from __future__ import annotations

import argparse
import codecs
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import secrets
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import NamedTuple

VERSION = 1
RANGE = ipaddress.IPv4Network("127.64.0.0/10")
SESSION_LOCK = ".session.lock"
STALE_SESSION_AGE = 24 * 60 * 60
CHILD_TERM_TIMEOUT = 5
CHILD_POLL_INTERVAL = 0.05
CHILD_REAP_TIMEOUT = 1
CHILD_RELAY_JOIN_TIMEOUT = 2
RELAY_CHUNK_SIZE = 64 * 1024
_emit_lock = threading.Lock()


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
    with _emit_lock:
        print(line, flush=True)


def error(message, code="HELPER_ERROR"):
    emit("ERROR", code=code, message=str(message))


def workspace_root():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / "zephyr_remote_openocd"
    return Path.home() / ".cache" / "zephyr_remote_openocd" / "sessions"


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
            if not path.is_dir() or path.stat().st_mtime > cutoff:
                continue
            if not lock_path.is_file():
                shutil.rmtree(path, ignore_errors=True)
                continue
            lock = lock_path.open("r+b")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            shutil.rmtree(path, ignore_errors=True)
        finally:
            lock.close()


def new_workspace():
    root = workspace_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    reclaim_stale_workspaces(root)
    for _ in range(32):
        session_id = secrets.token_urlsafe(18)
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
                kind == "file" and any(kinds.get(parent) == "file" for parent in path.parents)
                for path, kind in kinds.items()
            ) or any(
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
    finally:
        spool.close()
    emit(
        "STAGED",
        byte_count=count,
        sha256=digest.hexdigest(),
        files=names,
        directories=[str(relative) for _, relative, _, kind in validated if kind == "directory"],
    )


def random_address():
    # Exclude network/broadcast endpoints without material bias.
    return str(
        ipaddress.IPv4Address(
            int(RANGE.network_address) + 1 + secrets.randbelow(RANGE.num_addresses - 2)
        )
    )


def fake_child(address, ports):
    listeners = []
    try:
        for port in ports:
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((address, port))
            listener.listen()
            listeners.append(listener)
        print("ZRO_FAKE_READY", flush=True)
        print("fake service ready", file=sys.stderr, flush=True)
        selector = selectors.DefaultSelector()
        for listener in listeners:
            selector.register(listener, selectors.EVENT_READ)
        while True:
            for key, _ in selector.select():
                selected_listener = key.fileobj
                if not isinstance(selected_listener, socket.socket):
                    raise TypeError("selector returned a non-socket listener")
                connection, _ = selected_listener.accept()
                threading.Thread(target=echo, args=(connection,), daemon=True).start()
    finally:
        for listener in listeners:
            listener.close()


def echo(connection):
    with connection:
        while True:
            data = connection.recv(65536)
            if not data:
                return
            connection.sendall(data)


class _MarkerMatcher:
    """Recognize one complete trimmed marker line without retaining its text."""

    def __init__(self, marker, marker_seen):
        self.marker = marker
        self.marker_seen = marker_seen
        self._index = 0
        self._started = False
        self._valid = True

    def feed(self, character):
        if self.marker_seen.is_set() or not self._valid:
            return
        if not self._started and character.isspace():
            return
        if self._index < len(self.marker) and character == self.marker[self._index]:
            self._started = True
            self._index += 1
            return
        if self._index == len(self.marker) and character.isspace():
            return
        self._valid = False

    def finish_line(self):
        if (
            not self.marker_seen.is_set()
            and self._valid
            and self._started
            and self._index == len(self.marker)
        ):
            self.marker_seen.set()
        self._index = 0
        self._started = False
        self._valid = True


class _CapturedFragment:
    """Retained startup output with its stream and boundary metadata."""

    __slots__ = ("stream", "payload", "line_end")

    def __init__(self, stream, payload, line_end):
        self.stream = stream
        self.payload = payload
        self.line_end = line_end


def relay(stream, stream_name, marker=None, marker_seen=None, captured=None, capture_lock=None):
    """Relay bounded UTF-8 fragments while matching complete marker lines."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    matcher = (
        _MarkerMatcher(marker, marker_seen)
        if marker is not None and marker_seen is not None
        else None
    )
    pending: list[str] = []
    read_chunk = getattr(stream, "read1", None)
    if read_chunk is None:
        read_chunk = stream.read

    def emit_fragment(payload, *, line_end=False):
        if captured is not None:
            record = _CapturedFragment(stream_name, payload, line_end)
            if capture_lock is None:
                captured.append(record)
                del captured[:-128]
            else:
                with capture_lock:
                    captured.append(record)
                    del captured[:-128]
        emit(
            "CHILD_OUTPUT",
            stream=stream_name,
            payload=payload,
            line_end=line_end,
        )

    def consume(text):
        for character in text:
            if character == "\n":
                if matcher is not None:
                    matcher.finish_line()
                emit_fragment("".join(pending), line_end=True)
                pending.clear()
                continue
            if matcher is not None:
                matcher.feed(character)
            pending.append(character)
            if len(pending) >= RELAY_CHUNK_SIZE:
                emit_fragment("".join(pending))
                pending.clear()

    while True:
        chunk = read_chunk(RELAY_CHUNK_SIZE)
        if not chunk:
            consume(decoder.decode(b"", final=True))
            if matcher is not None:
                matcher.finish_line()
            if pending:
                emit_fragment("".join(pending))
            return
        consume(decoder.decode(chunk, final=False))


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
    for _ in range(32):
        address = random_address()
        sockets = []
        try:
            for port in ports:
                candidate = socket.socket()
                candidate.bind((address, port))
                sockets.append(candidate)
            return address
        except OSError:
            pass
        finally:
            for candidate in sockets:
                candidate.close()
    raise RuntimeError("loopback allocation exhausted after 32 attempts")


def services_connectable(address, services):
    """Probe non-GDB listeners without consuming an OpenOCD client slot.

    OpenOCD's GDB server treats a bare TCP connect as a rejected debugger
    session.  The real GDB client connection is therefore authoritative for
    that service; Tcl and telnet remain safe to probe here.
    """
    for service in services:
        if service.name == "gdb":
            continue
        try:
            with socket.create_connection((address, service.remote_port), timeout=0.2):
                pass
        except OSError:
            return False
    return True


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


def _validate_marker(marker):
    if marker is not None and (
        not isinstance(marker, str) or not marker or any(c.isspace() for c in marker)
    ):
        raise ValueError("START readiness marker is invalid")


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


def _validate_options(marker, timeout, literal_prefix, argv_length):
    _validate_marker(marker)
    _validate_timeout(timeout)
    _validate_literal_prefix(literal_prefix, argv_length)


def _expand(value, replacements):
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
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
    readiness_marker: str | None
    readiness_timeout: float
    literal_prefix: int


class StopRequest:
    """Marker for the parameterless STOP command."""


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
        "readiness_marker",
        "readiness_timeout",
        "literal_prefix",
    }
    if set(message) != fields:
        raise ValueError("START fields are invalid")
    argv = message["argv"]
    environment = message["environment"]
    marker = message["readiness_marker"]
    timeout = message["readiness_timeout"]
    literal_prefix = message["literal_prefix"]
    _validate_argv(argv)
    _validate_environment(environment)
    _validate_options(marker, timeout, literal_prefix, len(argv))
    services = _parse_services(message["services"], "START")
    checks = _parse_required_paths(message["required_paths"])
    return StartRequest(
        tuple(argv),
        tuple(environment.items()),
        checks,
        services,
        marker,
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


class SupervisedChild:
    """Own one child process and all resources used to relay its output."""

    def __init__(self, process, marker=None):
        self.process = process
        self.marker = marker
        self.marker_seen = threading.Event()
        self.startup_output: list[_CapturedFragment] = []
        self._capture_lock = threading.Lock()
        self.relay_threads: list[threading.Thread] = []
        self._observed_returncode = None

    @property
    def pid(self):
        return self.process.pid

    @property
    def returncode(self):
        if self._observed_returncode is not None:
            return self._observed_returncode
        return self.process.returncode

    def poll(self):
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

    def start_relays(self, capture_startup=False):
        if self.process.stdout is None or self.process.stderr is None:
            raise RuntimeError("child output was not captured")
        captured = self.startup_output if capture_startup else None
        self.relay_threads = [
            threading.Thread(
                target=relay,
                args=(
                    self.process.stdout,
                    "stdout",
                    self.marker,
                    self.marker_seen,
                    captured,
                    self._capture_lock,
                ),
                daemon=True,
            ),
            threading.Thread(
                target=relay,
                args=(
                    self.process.stderr,
                    "stderr",
                    self.marker,
                    self.marker_seen,
                    captured,
                    self._capture_lock,
                ),
                daemon=True,
            ),
        ]
        for thread in self.relay_threads:
            thread.start()

    def join_relays(self):
        deadline = time.monotonic() + CHILD_RELAY_JOIN_TIMEOUT
        for thread in self.relay_threads:
            # A signal may arrive before start_relays has started every thread.
            if thread.ident is None:
                continue
            if thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _active_relays(self):
        return tuple(thread for thread in self.relay_threads if thread.is_alive())

    def close_streams(self):
        active = self._active_relays()
        errors: list[BaseException] = []
        if active:
            names = ", ".join(thread.name for thread in active)
            errors.append(RuntimeError(f"child output relay did not stop ({names})"))
        for stream in (self.process.stdout, self.process.stderr):
            if stream is None or stream.closed:
                continue
            if active:
                continue
            try:
                stream.close()
            except BaseException as error:
                errors.append(error)
        _raise_cleanup_errors(errors)

    def dispose(self):
        self.join_relays()
        self.close_streams()

    def _group_exists(self):
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def _wait_for_leader_exit(self):
        deadline = time.monotonic() + CHILD_TERM_TIMEOUT
        while self.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(CHILD_POLL_INTERVAL, remaining))
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
            print(
                "warning: terminating remaining OpenOCD process-group members: "
                + ", ".join(map(str, members)),
                file=sys.stderr,
                flush=True,
            )

    def terminate(self):
        errors = []
        if self.process.returncode is None:
            group_exists = True
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                group_exists = False
            except BaseException as error:
                errors.append(error)
            if group_exists:
                try:
                    self._wait_for_leader_exit()
                except BaseException as error:
                    errors.append(error)
                try:
                    group_exists = self._group_exists()
                except BaseException as error:
                    errors.append(error)
                    group_exists = True
                if group_exists:
                    with suppress(BaseException):
                        self._warn_remaining_group_members()
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except BaseException as error:
                        errors.append(error)
            try:
                returncode = self.process.wait(timeout=CHILD_REAP_TIMEOUT)
                self._observed_returncode = returncode
            except BaseException as error:
                errors.append(error)
        try:
            self.dispose()
        except BaseException as error:
            errors.append(error)
        _raise_cleanup_errors(errors)


def _spawn_child(argv, *, cwd=None, environment=None, marker=None):
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    return SupervisedChild(process, marker)


def _expanded_argv(request, work, address):
    replacements = {"{workspace}": str(work), "{address}": address}
    return [
        arg if index < request.literal_prefix else _expand(arg, replacements)
        for index, arg in enumerate(request.argv)
    ], replacements


def _child_environment(request):
    environment = os.environ.copy()
    environment.update(dict(request.environment))
    return environment


def _wait_for_process(child, address, request, attempt):
    if request.readiness_marker is None:
        emit("PROCESS_READY", remote_address=address, child_pid=child.pid)
        return True
    deadline = time.monotonic() + request.readiness_timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            child.terminate()
            if is_bind_collision(child.startup_output) and attempt < 31:
                return False
            raise RuntimeError(f"process exited before readiness with status {child.returncode}")
        if child.marker_seen.is_set() and services_connectable(address, request.services):
            emit("PROCESS_READY", remote_address=address, child_pid=child.pid)
            return True
        time.sleep(0.05)
    raise RuntimeError("process readiness timed out")


class ControlSession:
    """Own the control connection, workspace, child, and lifecycle cleanup."""

    def __init__(self, session_id, work, workspace_lock):
        self.session_id = session_id
        self.work = work
        self.workspace_lock = workspace_lock
        self.child: SupervisedChild | None = None
        self.protocol_error: BaseException | None = None
        self.stopping = False

    @classmethod
    def create(cls):
        return cls(*new_workspace())

    def announce(self):
        emit(
            "SESSION_CREATED",
            helper="zephyr_remote_openocd",
            session_id=self.session_id,
            remote_workspace=str(self.work),
        )

    def _start_process(self, request):
        if self.child is not None:
            raise ValueError("START is only valid once")
        ports = [service.remote_port for service in request.services]
        attempts = 32 if request.readiness_marker is not None else 1
        for attempt in range(attempts):
            address = allocate_service_address(ports) if ports else random_address()
            argv, replacements = _expanded_argv(request, self.work, address)
            _check_required_paths(request.required_paths, replacements)
            self.child = _spawn_child(
                argv,
                cwd=self.work / "staged",
                environment=_child_environment(request),
                marker=request.readiness_marker,
            )
            self.child.start_relays(capture_startup=True)
            if _wait_for_process(self.child, address, request, attempt):
                return
            self.child = None
        raise RuntimeError("process address collision retry exhausted after 32 attempts")

    def dispatch(self, message):
        request = decode_command(message)
        if isinstance(request, StartRequest):
            self._start_process(request)
            return True
        if isinstance(request, StopRequest):
            self.cleanup()
            emit("SESSION_CLOSED", reason="requested", returncode=None)
            return False
        raise ValueError(f"unexpected request: {request!r}")

    def _child_finished(self):
        if self.child is None or self.child.poll() is None:
            return False
        returncode = self.child.returncode
        self.cleanup()
        emit("SESSION_CLOSED", reason="process_exit", returncode=returncode)
        return True

    def _read_and_dispatch(self):
        line = sys.stdin.buffer.readline()
        if not line:
            return False
        try:
            message = json.loads(line)
            return self.dispatch(message)
        except Exception as exc:
            self.protocol_error = exc
            return False

    def run(self):
        operation_error = None
        cleanup_error = None
        try:
            try:
                self.announce()
                selector = selectors.DefaultSelector()
                try:
                    selector.register(sys.stdin.buffer, selectors.EVENT_READ)
                    while not self._child_finished():
                        if selector.select(0.2) and not self._read_and_dispatch():
                            break
                finally:
                    selector.close()
            except BaseException as exc:
                operation_error = exc
        finally:
            try:
                self.cleanup()
            except BaseException as exc:
                cleanup_error = exc

        if self.protocol_error is not None:
            if operation_error is not None:
                self.protocol_error.add_note(f"session operation also failed: {operation_error}")
            if cleanup_error is not None:
                self.protocol_error.add_note(f"session cleanup also failed: {cleanup_error}")
            error(self.protocol_error, "PROTOCOL_ERROR")
            if operation_error is not None or cleanup_error is not None:
                raise SystemExit(1)
            return
        if operation_error is not None:
            if cleanup_error is not None:
                operation_error.add_note(f"session cleanup also failed: {cleanup_error}")
            raise operation_error
        if cleanup_error is not None:
            raise cleanup_error

    def handle_signal(self, *_):
        self.cleanup()
        raise SystemExit(0)

    def cleanup(self):
        if self.stopping:
            return
        errors = []
        if self.child is not None:
            try:
                self.child.terminate()
            except BaseException as error:
                errors.append(error)
        try:
            shutil.rmtree(self.work)
        except FileNotFoundError as error:
            if self.work.exists():
                errors.append(error)
        except BaseException as error:
            errors.append(error)
        try:
            self.workspace_lock.close()
        except BaseException as error:
            errors.append(error)
        self.stopping = True
        if errors:
            _raise_cleanup_errors(errors)


def control():
    session = ControlSession.create()
    signal.signal(signal.SIGTERM, session.handle_signal)
    signal.signal(signal.SIGINT, session.handle_signal)
    session.run()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("control")
    staging = sub.add_parser("stage")
    staging.add_argument("workspace")
    version = sub.add_parser("openocd-version")
    version.add_argument("executable", nargs="+")
    fake = sub.add_parser("fake-child")
    fake.add_argument("address")
    fake.add_argument("ports", type=int, nargs="+")
    args = parser.parse_args()
    if args.command == "control":
        control()
    elif args.command == "stage":
        stage(args.workspace)
    elif args.command == "openocd-version":
        openocd_version(args.executable)
    else:
        fake_child(args.address, args.ports)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error(exc)
        raise SystemExit(1) from exc
