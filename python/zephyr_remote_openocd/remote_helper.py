#!/usr/bin/env python3
"""Protocol-1 remote helper.  This file is deliberately self-contained."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
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

VERSION = 1
RANGE = ipaddress.IPv4Network("127.64.0.0/10")


def emit(kind, **values):
    print(json.dumps({"version": VERSION, "type": kind, **values}, separators=(",", ":"), sort_keys=True), flush=True)


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
    if not member.name or member.name == "." or path.is_absolute() or any(p in ("", ".", "..") for p in path.parts):
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
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
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
    return str(ipaddress.IPv4Address(int(RANGE.network_address) + 1 + secrets.randbelow(RANGE.num_addresses - 2)))


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


def relay(stream, stream_name):
    while True:
        line = stream.readline()
        if not line:
            return
        emit("CHILD_OUTPUT", stream=stream_name, payload=line.decode("utf-8", "replace").rstrip("\n"))


def controller():
    session_id, work = new_workspace()
    child = None
    stopping = False

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
        shutil.rmtree(work, ignore_errors=True)

    signal.signal(signal.SIGTERM, lambda *_: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(0)))
    try:
        emit("HELLO", helper="zephyr-remote-openocd")
        emit("SESSION_CREATED", session_id=session_id, remote_workspace=str(work))
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin.buffer, selectors.EVENT_READ)
        while True:
            if child is not None and child.poll() is not None:
                emit("PROCESS_EXIT", returncode=child.returncode)
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
                    if any(isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535 for p in ports):
                        raise ValueError("invalid remote service port")
                    for attempt in range(32):
                        address = random_address()
                        child = subprocess.Popen(
                            [sys.executable, str(Path(__file__).resolve()), "fake-child", address, *map(str, ports)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
                    threading.Thread(target=relay, args=(child.stdout, "stdout"), daemon=True).start()
                    threading.Thread(target=relay, args=(child.stderr, "stderr"), daemon=True).start()
                    emit("SERVICE_READY", remote_address=address, services=services, child_pid=child.pid)
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
    fake = sub.add_parser("fake-child")
    fake.add_argument("address")
    fake.add_argument("ports", type=int, nargs="+")
    args = parser.parse_args()
    if args.command == "control":
        controller()
    elif args.command == "stage":
        stage(args.workspace)
    else:
        fake_child(args.address, args.ports)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error(exc)
        raise SystemExit(1)
