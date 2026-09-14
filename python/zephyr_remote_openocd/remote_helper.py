#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Protocol v1 remote helper. This file is deliberately self-contained."""

from __future__ import annotations

import argparse
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
from pathlib import Path, PurePosixPath
from typing import NamedTuple

VERSION = 1
RANGE = ipaddress.IPv4Network("127.64.0.0/10")
SESSION_LOCK = ".session.lock"
STALE_SESSION_AGE = 24 * 60 * 60
_emit_lock = threading.Lock()


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
    path = PurePosixPath(member.name)
    if (
        not member.name
        or member.name == "."
        or path.is_absolute()
        or any(p in ("", ".", "..") for p in path.parts)
    ):
        raise ValueError(f"unsafe archive path: {member.name!r}")
    if path in seen:
        raise ValueError(f"duplicate archive path: {path}")
    seen.add(path)
    if not member.isreg():
        raise ValueError(f"archive member is not a regular file: {path}")
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
            for member in members:
                relative = valid_member(member, seen)
                target = target_root.joinpath(*relative.parts)
                if target_root.resolve() not in target.resolve().parents:
                    raise ValueError(f"archive path escapes staging directory: {relative}")
                validated.append((member, relative, target))
            for member, relative, target in validated:
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
    emit("STAGED", byte_count=count, sha256=digest.hexdigest(), files=names)


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


def relay(stream, stream_name, marker=None, marker_seen=None, captured=None):
    while True:
        line = stream.readline()
        if not line:
            return
        payload = line.decode("utf-8", "replace").rstrip("\n")
        if captured is not None:
            captured.append(payload)
            del captured[:-128]
        if marker is not None and payload.strip() == marker:
            marker_seen.set()
        emit("CHILD_OUTPUT", stream=stream_name, payload=payload)


def is_bind_collision(output):
    """Recognize the POSIX EADDRINUSE diagnostic from OpenOCD startup."""
    return "address already in use" in "\n".join(output).casefold()


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
        self.startup_output: list[str] = []
        self.relay_threads: list[threading.Thread] = []

    @property
    def pid(self):
        return self.process.pid

    @property
    def returncode(self):
        return self.process.returncode

    def poll(self):
        return self.process.poll()

    def start_relays(self, capture_startup=False):
        if self.process.stdout is None or self.process.stderr is None:
            raise RuntimeError("child output was not captured")
        captured = self.startup_output if capture_startup else None
        self.relay_threads = [
            threading.Thread(
                target=relay,
                args=(self.process.stdout, "stdout", self.marker, self.marker_seen, captured),
                daemon=True,
            ),
            threading.Thread(
                target=relay,
                args=(self.process.stderr, "stderr", self.marker, self.marker_seen, captured),
                daemon=True,
            ),
        ]
        for thread in self.relay_threads:
            thread.start()

    def join_relays(self):
        for thread in self.relay_threads:
            # A signal may arrive before start_relays has started every thread.
            if thread.is_alive():
                thread.join(timeout=2)

    def close_streams(self):
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def dispose(self):
        self.join_relays()
        self.close_streams()

    def terminate(self):
        if self.poll() is None:
            try:
                os.killpg(self.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.poll() is None:
                    os.killpg(self.pid, signal.SIGKILL)
                    self.process.wait()
        self.dispose()


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
            child.dispose()
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
        self.child.dispose()
        emit("SESSION_CLOSED", reason="process_exit", returncode=self.child.returncode)
        return True

    def _read_and_dispatch(self):
        line = sys.stdin.buffer.readline()
        if not line:
            return False
        try:
            message = json.loads(line)
            return self.dispatch(message)
        except Exception as exc:
            error(exc, "PROTOCOL_ERROR")
            return False

    def run(self):
        try:
            self.announce()
            selector = selectors.DefaultSelector()
            try:
                selector.register(sys.stdin.buffer, selectors.EVENT_READ)
                while not self._child_finished():
                    if selector.select(0.2) and not self._read_and_dispatch():
                        return
            finally:
                selector.close()
        finally:
            self.cleanup()

    def handle_signal(self, *_):
        self.cleanup()
        raise SystemExit(0)

    def cleanup(self):
        if self.stopping:
            return
        self.stopping = True
        if self.child is not None:
            self.child.terminate()
        shutil.rmtree(self.work, ignore_errors=True)
        self.workspace_lock.close()


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
