#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0

"""Protocol-1 remote helper.  This file is deliberately self-contained."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
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

VERSION = 1
RANGE = ipaddress.IPv4Network("127.64.0.0/10")
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
        return Path(runtime) / "zephyr-remote-openocd"
    return Path.home() / ".cache" / "zephyr-remote-openocd" / "sessions"


def new_workspace():
    root = workspace_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for _ in range(32):
        session_id = secrets.token_urlsafe(18)
        path = root / session_id
        try:
            path.mkdir(mode=0o700)
            (path / "staged").mkdir(mode=0o700)
            return session_id, path
        except FileExistsError:
            pass
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
            seen = set()
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
        print(json.dumps({"ready": True}), flush=True)
        print("fake service ready", file=sys.stderr, flush=True)
        selector = selectors.DefaultSelector()
        for listener in listeners:
            selector.register(listener, selectors.EVENT_READ)
        while True:
            for key, _ in selector.select():
                connection, _ = key.fileobj.accept()
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


def relay(stream, stream_name, marker=None, marker_seen=None):
    while True:
        line = stream.readline()
        if not line:
            return
        payload = line.decode("utf-8", "replace").rstrip("\n")
        if marker is not None and payload.strip() == marker:
            marker_seen.set()
        emit("CHILD_OUTPUT", stream=stream_name, payload=payload)


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


def services_connectable(address, ports):
    for port in ports:
        try:
            with socket.create_connection((address, port), timeout=0.2):
                pass
        except OSError:
            return False
    return True


def openocd_version(executable):
    if not Path(executable).is_absolute():
        raise ValueError("OpenOCD executable must be an absolute path")
    result = subprocess.run(
        [executable, "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode:
        raise RuntimeError(f"OpenOCD version query failed ({result.returncode}): {output.strip()}")
    emit("OPENOCD_VERSION", output=output)


def controller():
    session_id, work = new_workspace()
    child = None
    relay_threads = []
    stopping = False

    def close_child_streams():
        if child is not None:
            for stream in (child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def cleanup(*_):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        for thread in relay_threads:
            thread.join(timeout=2)
        close_child_streams()
        shutil.rmtree(work, ignore_errors=True)

    def handle_signal(*_):
        cleanup()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        emit("HELLO", helper="zephyr-remote-openocd")
        emit("SESSION_CREATED", session_id=session_id, remote_workspace=str(work))
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin.buffer, selectors.EVENT_READ)
        while True:
            if child is not None and child.poll() is not None:
                for thread in relay_threads:
                    thread.join(timeout=2)
                close_child_streams()
                emit("PROCESS_EXIT", returncode=child.returncode)
                emit("STOPPED", reason="process-exit")
                return
            ready_inputs = selector.select(0.2)
            if not ready_inputs:
                continue
            line = sys.stdin.buffer.readline()
            if not line:
                return
            try:
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("version") != VERSION:
                    raise ValueError("incompatible or missing protocol version")
                kind = message.get("type")
                if kind == "START":
                    if child is not None:
                        raise ValueError("START is only valid once")
                    services = message.get("services")
                    if not isinstance(services, list) or not services:
                        raise ValueError("START requires services")
                    ports = [item["remote_port"] for item in services]
                    if any(
                        isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535
                        for p in ports
                    ):
                        raise ValueError("invalid remote service port")
                    for _attempt in range(32):
                        address = random_address()
                        child = subprocess.Popen(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "fake-child",
                                address,
                                *map(str, ports),
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            start_new_session=True,
                        )
                        ready = child.stdout.readline()
                        try:
                            ready_message = json.loads(ready) if ready else {}
                        except json.JSONDecodeError:
                            ready_message = {}
                        if ready_message.get("ready") is True:
                            break
                        child.wait()
                        child = None
                    else:
                        raise RuntimeError("loopback allocation exhausted after 32 attempts")
                    threading.Thread(
                        target=relay, args=(child.stdout, "stdout"), daemon=True
                    ).start()
                    threading.Thread(
                        target=relay, args=(child.stderr, "stderr"), daemon=True
                    ).start()
                    emit(
                        "SERVICE_READY",
                        remote_address=address,
                        services=services,
                        child_pid=child.pid,
                    )
                elif kind == "START_OPENOCD":
                    if child is not None:
                        raise ValueError("a child process is already running")
                    argv = message.get("argv")
                    environment = message.get("environment", {})
                    checks = message.get("required_paths", [])
                    services = message.get("services", [])
                    marker = message.get("readiness_marker")
                    readiness_timeout = message.get("readiness_timeout", 30.0)
                    if (
                        not isinstance(argv, list)
                        or not argv
                        or not all(isinstance(arg, str) and arg for arg in argv)
                    ):
                        raise ValueError("START_OPENOCD requires a non-empty string argv")
                    if not isinstance(environment, dict) or not all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in environment.items()
                    ):
                        raise ValueError("START_OPENOCD environment must contain string values")
                    if not isinstance(services, list) or not all(
                        isinstance(item, dict)
                        and isinstance(item.get("name"), str)
                        and isinstance(item.get("remote_port"), int)
                        and not isinstance(item.get("remote_port"), bool)
                        and 1 <= item["remote_port"] <= 65535
                        for item in services
                    ):
                        raise ValueError("START_OPENOCD services are invalid")
                    if marker is not None and (
                        not isinstance(marker, str)
                        or not marker
                        or any(c.isspace() for c in marker)
                    ):
                        raise ValueError("START_OPENOCD readiness marker is invalid")
                    if (
                        not isinstance(readiness_timeout, (int, float))
                        or isinstance(readiness_timeout, bool)
                        or readiness_timeout <= 0
                    ):
                        raise ValueError("START_OPENOCD readiness timeout is invalid")
                    ports = [item["remote_port"] for item in services]
                    address = allocate_service_address(ports) if ports else random_address()
                    replacements = {"{workspace}": str(work), "{address}": address}

                    def expand(value, replacements=replacements):
                        for token, replacement in replacements.items():
                            value = value.replace(token, replacement)
                        return value

                    expanded_argv = [expand(arg) for arg in argv]
                    for check in checks:
                        if (
                            not isinstance(check, dict)
                            or check.get("kind") not in ("file", "directory")
                            or not isinstance(check.get("path"), str)
                        ):
                            raise ValueError("invalid required-path assertion")
                        candidate = Path(expand(check["path"]))
                        valid = (
                            candidate.is_file() if check["kind"] == "file" else candidate.is_dir()
                        )
                        if not valid:
                            raise ValueError(
                                f"required remote {check['kind']} is missing: {candidate}"
                            )
                    child_environment = os.environ.copy()
                    child_environment.update(environment)
                    child = subprocess.Popen(
                        expanded_argv,
                        cwd=work / "staged",
                        env=child_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                    )
                    marker_seen = threading.Event()
                    relay_threads = [
                        threading.Thread(
                            target=relay,
                            args=(child.stdout, "stdout", marker, marker_seen),
                            daemon=True,
                        ),
                        threading.Thread(
                            target=relay,
                            args=(child.stderr, "stderr", marker, marker_seen),
                            daemon=True,
                        ),
                    ]
                    emit("PROCESS_STARTED", remote_address=address, child_pid=child.pid)
                    for thread in relay_threads:
                        thread.start()
                    if marker is not None:
                        deadline = time.monotonic() + readiness_timeout
                        while time.monotonic() < deadline:
                            if child.poll() is not None:
                                raise RuntimeError(
                                    "OpenOCD exited before readiness with status "
                                    f"{child.returncode}"
                                )
                            if marker_seen.is_set() and services_connectable(address, ports):
                                for service in services:
                                    emit("SERVICE_READY", remote_address=address, service=service)
                                break
                            time.sleep(0.05)
                        else:
                            raise RuntimeError("OpenOCD readiness timed out")
                elif kind == "STOP":
                    cleanup()
                    emit("STOPPED", reason="requested")
                    return
                else:
                    raise ValueError(f"unexpected command: {kind!r}")
            except Exception as exc:
                error(exc, "PROTOCOL_ERROR")
                return
    finally:
        cleanup()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("control")
    staging = sub.add_parser("stage")
    staging.add_argument("workspace")
    version = sub.add_parser("openocd-version")
    version.add_argument("executable")
    fake = sub.add_parser("fake-child")
    fake.add_argument("address")
    fake.add_argument("ports", type=int, nargs="+")
    args = parser.parse_args()
    if args.command == "control":
        controller()
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
