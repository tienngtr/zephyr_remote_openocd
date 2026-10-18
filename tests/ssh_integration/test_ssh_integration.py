# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import io
import ipaddress
import shlex
import shutil
import socket
import tempfile
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import override

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.controller._deployment import build_helper_bundle, deploy_helper
from zephyr_remote_openocd.controller._forwarding import ForwardManager
from zephyr_remote_openocd.controller._ssh import (
    ManagedSshProcess,
    ProcessAcquiredHandler,
    SshCommand,
    SshForwardSpec,
)
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionController,
    SessionError,
    SessionPlan,
    StagedFile,
)
from zephyr_remote_openocd.controller.paths import PathPlanner
from zephyr_remote_openocd.protocol.arguments import ArgumentTemplate, SessionValue
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.protocol.responses import validate_openocd_version_response
from zephyr_remote_openocd.protocol.wire import decode_frame

from tests.inventory import InventoryHost
from tests.process_support import cleanup_on_exit, read_line, stop_and_close
from tests.support import ROOT

pytestmark = pytest.mark.ssh
REMOTE_FAILURE_EXIT_CODE = 7

# Sent as remote ``python3 -c`` source text; see that module for its arguments.
REMOTE_SESSION_ECHO = (ROOT / "tests/fixtures/remote_session_echo.py").read_text(encoding="utf-8")


def session_echo_process() -> ProcessSpec:
    return ProcessSpec(
        ("python3", "-c", REMOTE_SESSION_ECHO, "{address}", "3333"),
        required_output_sentinels=("ZRO_TEST_READY",),
        literal_prefix=3,
        argv_templates=((3, ArgumentTemplate((SessionValue.ADDRESS,))),),
    )


def assert_remote_marker(ssh: SshCommand, host: str) -> None:
    result = ssh.run(host, "printf zro_ssh_marker", timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b"zro_ssh_marker"


def free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def assert_no_loopback_listener(port: int) -> None:
    """Reject a live listener while allowing settled connections in TIME_WAIT."""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def assert_echo(port: int, payload: bytes) -> None:
    """Use an endpoint the session has already reported ready; never retry."""
    with socket.create_connection(("127.0.0.1", port), timeout=20) as connection:
        connection.sendall(payload)
        received = b""
        while len(received) < len(payload):
            chunk = connection.recv(len(payload) - len(received))
            if not chunk:
                break
            received += chunk
        assert received == payload


class ObservedSshCommand(SshCommand):
    """Borrow transport references for loss injection; sessions retain cleanup ownership."""

    helper_processes: list[ManagedSshProcess]
    forwards: list[ManagedSshProcess]

    @override
    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "helper_processes", [])
        object.__setattr__(self, "forwards", [])

    @override
    def popen(
        self,
        host: str,
        remote_command: str,
        *,
        local_forward: SshForwardSpec | None = None,
        on_acquired: ProcessAcquiredHandler | None = None,
    ) -> ManagedSshProcess:
        process = super().popen(
            host, remote_command, local_forward=local_forward, on_acquired=on_acquired
        )
        if local_forward is not None:
            self.forwards.append(process)
        elif remote_command.endswith(" control"):
            self.helper_processes.append(process)
        return process


class TestConfiguredSshIntegration:
    @pytest.fixture(autouse=True)
    def inventory_setup(self, ssh_host: str, ssh_settings: InventoryHost) -> None:
        self.host = ssh_host
        self.ssh = SshCommand(ssh_settings.ssh_command)

    def test_configured_ssh_and_fixed_arguments(self) -> None:
        assert_remote_marker(self.ssh, self.host)
        assert_remote_marker(
            SshCommand((*self.ssh.argv_prefix, "-o", "ConnectTimeout=10")), self.host
        )

    def test_explicit_path_with_nonstandard_executable_name(self, tmp_path: Path) -> None:
        configured = self.ssh.argv_prefix[0]
        source = shutil.which(configured) if "/" not in configured else configured
        assert source is not None, f"configured SSH executable not found: {configured}"
        alternate = tmp_path / "custom-ssh"
        alternate.symlink_to(Path(source).resolve())
        command = SshCommand((str(alternate), *self.ssh.argv_prefix[1:]))

        assert_remote_marker(command, self.host)


class TestSshTransportIntegration:
    @pytest.fixture(autouse=True)
    def inventory_setup(self, ssh_host: str, ssh_settings: InventoryHost) -> None:
        self.host = ssh_host
        self.ssh_settings = ssh_settings
        self.ssh = SshCommand(ssh_settings.ssh_command)

    @pytest.mark.parametrize("occupied", (False, True), ids=("available", "occupied"))
    def test_forwarding_conflicts_cannot_report_an_unbound_forward_ready(
        self, occupied: bool
    ) -> None:
        conflicts = ("-o", "ExitOnForwardFailure=no", "-o", "ClearAllForwardings=yes")
        ssh = SshCommand((*self.ssh.argv_prefix, *conflicts))
        if occupied:
            assert_remote_marker(ssh, self.host)
        manager = ForwardManager(ssh, self.host)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            if occupied:
                listener.listen()
            else:
                listener.close()
            forward = ForwardSpec("gdb", port, 3333)
            try:
                if occupied:
                    with pytest.raises(SessionError):
                        manager.start((forward,), "127.0.0.1")
                    assert not manager.has_forwards
                else:
                    manager.start((forward,), "127.0.0.1")
                    assert manager.forwards == (forward,)
                    # Verify a listener exists immediately after actual readiness.
                    with socket.create_connection(("127.0.0.1", port), timeout=20):
                        pass
            finally:
                manager.close()
            assert not manager.has_forwards
            if not occupied:
                assert_no_loopback_listener(port)

    def test_streaming_preserves_content_and_reports_remote_failure(self) -> None:
        payloads = (
            b"",
            bytes(range(256)) * 4096,
        )
        command = (
            "python3 -c 'import hashlib,sys; d=sys.stdin.buffer.read(); "
            "print(len(d), hashlib.sha256(d).hexdigest())'"
        )
        for payload in payloads:
            if payload:
                with tempfile.TemporaryFile("w+b") as stream:
                    stream.write(payload)
                    stream.seek(0)
                    result = self.ssh.run_stream(self.host, command, stream, timeout=30)
            else:
                # An empty buffer must deliver EOF rather than inherit stdin.
                result = self.ssh.run(self.host, command, input_data=payload, timeout=30)
            expected = f"{len(payload)} {hashlib.sha256(payload).hexdigest()}\n".encode()
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            assert result.stdout == expected

        failed = self.ssh.run(
            self.host,
            "python3 -c 'import sys; sys.stdin.buffer.read(); "
            f"raise SystemExit({REMOTE_FAILURE_EXIT_CODE})'",
            input_data=b"stream before remote failure",
            timeout=20,
        )
        assert failed.returncode == REMOTE_FAILURE_EXIT_CODE

    def test_protocol_v1_helper_vertical_slice(self) -> None:
        """Exercise the production transport path with an explicit test process."""
        first = deploy_helper(self.ssh, self.host)
        second = deploy_helper(self.ssh, self.host)
        assert first.path == second.path
        assert second.reused
        local_port = free_loopback_port()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "payload.bin"
            source.write_bytes(bytes(range(256)) + b"\0remote")
            request = SessionPlan(
                self.host,
                self.ssh,
                session_echo_process(),
                (StagedFile(source, PurePosixPath("nested/payload.bin")),),
                (ForwardSpec("gdb", local_port, 3333),),
            )
            session = SessionController.prepare(request)
            with cleanup_on_exit(session.close):
                session.acquire()
                assert session.descriptor is not None
                descriptor = session.descriptor
                workspace = descriptor.remote_workspace
                address = ipaddress.ip_address(descriptor.remote_address)
                assert address in ipaddress.ip_network("127.64.0.0/10")
                check = self.ssh.run(
                    self.host,
                    "python3 -c "
                    + shlex.quote(
                        "import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                        "print(oct(p.stat().st_mode&0o777), "
                        "(p/'staged/nested/payload.bin').stat().st_size)"
                    )
                    + " "
                    + shlex.quote(descriptor.remote_workspace),
                    timeout=20,
                )
                assert check.returncode == 0, check.stderr.decode(errors="replace")
                assert check.stdout.strip() == b"0o700 263"
                assert_echo(local_port, b"helper_round_trip")
            gone = self.ssh.run(
                self.host,
                "python3 -c "
                + shlex.quote(
                    "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                    "artifacts=(p, p.with_name('.'+p.name+'.lease'), "
                    "p.with_name('.'+p.name+'.closed')); "
                    "sys.exit(any(path.exists() for path in artifacts))"
                )
                + " "
                + shlex.quote(workspace),
                timeout=20,
            )
            assert gone.returncode == 0, gone.stderr.decode(errors="replace")

    def test_concurrent_sessions_isolate_identical_remote_ports(self) -> None:
        """Regression coverage for independent-session service-port isolation."""
        first_port = free_loopback_port()
        second_port = free_loopback_port()
        while second_port == first_port:
            second_port = free_loopback_port()
        with ExitStack() as resources:
            first = SessionController.prepare(
                SessionPlan(
                    self.host,
                    self.ssh,
                    session_echo_process(),
                    forwards=(ForwardSpec("gdb", first_port, 3333),),
                )
            )
            resources.enter_context(cleanup_on_exit(first.close))
            first.acquire()
            second = SessionController.prepare(
                SessionPlan(
                    self.host,
                    self.ssh,
                    session_echo_process(),
                    forwards=(ForwardSpec("gdb", second_port, 3333),),
                )
            )
            resources.enter_context(cleanup_on_exit(second.close))
            second.acquire()
            assert first.descriptor is not None
            first_descriptor = first.descriptor
            second_descriptor = second.descriptor
            assert second_descriptor is not None
            assert first_descriptor.remote_address != second_descriptor.remote_address
            assert first_descriptor.session_id != second_descriptor.session_id
            # Allocation alone does not prove both forwarded services remain usable.
            assert_echo(first_port, b"first_session_still_active")
            assert_echo(second_port, b"second_session_active")
        for descriptor in (first_descriptor, second_descriptor):
            result = self.ssh.run(
                self.host,
                f"test ! -e {shlex.quote(descriptor.remote_workspace)}",
                timeout=20,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")

    def test_post_spawn_bind_collision_fails_without_repeating_startup(self) -> None:
        """Real EADDRINUSE cannot authorize replay through the deployed helper."""
        process = ProcessSpec(
            (
                "python3",
                "-c",
                REMOTE_SESSION_ECHO,
                "{address}",
                "3333",
                "{workspace}/first-address",
            ),
            required_output_sentinels=("ZRO_TEST_READY",),
            literal_prefix=3,
            argv_templates=(
                (3, ArgumentTemplate((SessionValue.ADDRESS,))),
                (5, ArgumentTemplate((SessionValue.WORKSPACE, "/first-address"))),
            ),
        )
        forward = ForwardSpec("gdb", free_loopback_port(), 3333)
        attempts: list[tuple[str, ...]] = []
        output: list[str] = []
        with pytest.raises(SessionError):
            session = SessionController.prepare(
                SessionPlan(self.host, self.ssh, process, forwards=(forward,))
            )
            with cleanup_on_exit(session.close):
                session.acquire(
                    process_start_handler=attempts.append,
                    output_handler=lambda _stream, text, _line_end: output.append(text),
                )
        assert len(attempts) == 1
        assert "".join(output).count("ZRO_TEST_STARTUP_WORK") == 1
        assert "address already in use" in "".join(output).casefold()
        workspace = str(PurePosixPath(attempts[0][5]).parent)
        result = self.ssh.run(self.host, f"test ! -e {shlex.quote(workspace)}", timeout=20)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        with socket.socket() as local_listener:
            local_listener.bind(("127.0.0.1", forward.local_port))

    def test_helper_revision_repair_preserves_active_session(self) -> None:
        """Deploy and repair a distinct bundle while the current revision is live."""
        request = SessionPlan(
            self.host,
            self.ssh,
            session_echo_process(),
            forwards=(ForwardSpec("gdb", free_loopback_port(), 3333),),
        )
        session = SessionController.prepare(request)
        with cleanup_on_exit(session.close), ExitStack() as resources:
            session.acquire()
            bundle = io.BytesIO(build_helper_bundle())
            with zipfile.ZipFile(bundle, "a") as archive:
                archive.comment = uuid.uuid4().hex.encode("ascii")
            source = bundle.getvalue()
            digest = hashlib.sha256(source).hexdigest()
            revision_path = str(
                PurePosixPath(session.deployment.path).with_name(f"helper-{digest}.py")
            )

            def remove_revision() -> None:
                result = self.ssh.run(self.host, "rm -f " + shlex.quote(revision_path), timeout=20)
                assert result.returncode == 0, result.stderr.decode(errors="replace")

            # Own the unique test revision before deployment, including a lost reply.
            resources.enter_context(cleanup_on_exit(remove_revision))
            deployed = deploy_helper(self.ssh, self.host, source=source)
            assert deployed.path == revision_path
            assert deployed.path != session.deployment.path
            assert not deployed.reused
            assert deploy_helper(self.ssh, self.host, source=source).reused
            damaged = self.ssh.run(
                self.host,
                "python3 -c "
                + shlex.quote(
                    "import pathlib,sys; "
                    "pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
                )
                + " "
                + shlex.quote(revision_path),
                input_data=b"incomplete cached revision",
                timeout=20,
            )
            assert damaged.returncode == 0, damaged.stderr.decode(errors="replace")
            repaired = deploy_helper(self.ssh, self.host, source=source)
            assert repaired.path == deployed.path and not repaired.reused
            response = self.ssh.run(
                self.host,
                shlex.join(
                    (
                        "python3",
                        repaired.path,
                        "openocd-version",
                        "--",
                        *self.ssh_settings.openocd_command,
                    )
                ),
                timeout=20,
            )
            assert response.returncode == 0, response.stderr.decode(errors="replace")
            event = decode_frame(response.stdout)
            validate_openocd_version_response(event)
            assert event["type"] == "OPENOCD_VERSION"
            assert_echo(request.forwards[0].local_port, b"active_revision_survived")

    def test_preferred_address_reuses_forward_retained_by_external_master(
        self, tmp_path: Path
    ) -> None:
        """The test owns the master; production sessions own only their slaves."""
        ssh = SshCommand(
            (
                self.ssh.argv_prefix[0],
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=no",
                "-o",
                f"ControlPath={tmp_path / 'master'}",
                *self.ssh.argv_prefix[1:],
            )
        )
        local_port = free_loopback_port()
        request = SessionPlan(
            self.host,
            ssh,
            session_echo_process(),
            forwards=(ForwardSpec("gdb", local_port, 3333),),
        )
        master = ssh.popen(self.host, "printf 'ZRO_MASTER_READY\\n'; cat >/dev/null")
        with cleanup_on_exit(lambda: stop_and_close(master)):
            assert master.stdout is not None
            assert read_line(master.stdout).strip() == b"ZRO_MASTER_READY", master.stderr_tail()
            first = SessionController.prepare(request)
            with cleanup_on_exit(first.close):
                first.acquire()
                assert first.descriptor is not None
                address = first.descriptor.remote_address
                assert_echo(local_port, b"first_session")
            assert master.poll() is None
            with socket.create_connection(("127.0.0.1", local_port), timeout=20):
                pass
            second = SessionController.prepare(request)
            with cleanup_on_exit(second.close):
                second.acquire()
                assert second.descriptor is not None
                assert second.descriptor.remote_address == address
                assert_echo(local_port, b"reused_session")
            assert master.poll() is None
            if master.stdin is not None:
                master.stdin.close()
        assert_no_loopback_listener(local_port)

    def test_helper_ssh_loss_cleans_session(self) -> None:
        """Losing the helper SSH process cleans the session."""

        local_port = free_loopback_port()
        ssh = ObservedSshCommand(self.ssh_settings.ssh_command)
        session = SessionController.prepare(
            SessionPlan(
                self.host,
                ssh,
                session_echo_process(),
                forwards=(ForwardSpec("gdb", local_port, 3333),),
            )
        )
        try:
            session.acquire()
            assert session.descriptor is not None
            descriptor = session.descriptor
            helper_process = ssh.helper_processes[0]
            helper_process.terminate()
            helper_process.wait(timeout=20)
            with pytest.raises(SessionError):
                session.wait_for_openocd_exit(timeout=20)
        finally:
            with pytest.raises(SessionError):
                session.close()
        assert session.closed
        result = self.ssh.run(
            self.host,
            f"test ! -e {shlex.quote(descriptor.remote_workspace)}",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

    def test_owned_forward_loss_is_reported_and_session_cleans_up(self) -> None:
        """An established session reports loss of its own SSH forward."""

        local_port = free_loopback_port()
        ssh = ObservedSshCommand(self.ssh_settings.ssh_command)
        session = SessionController.prepare(
            SessionPlan(
                self.host,
                ssh,
                session_echo_process(),
                forwards=(ForwardSpec("gdb", local_port, 3333),),
            )
        )
        try:
            session.acquire()
            assert session.descriptor is not None
            descriptor = session.descriptor
            forward = ssh.forwards[0]
            forward.terminate()
            forward.wait(timeout=20)

            with pytest.raises(SessionError) as raised:
                session.poll_operation()
            assert "gdb" in str(raised.value)
            assert f"127.0.0.1:{local_port}" in str(raised.value)

            session.close()
        finally:
            session.close()

        assert session.closed
        result = self.ssh.run(
            self.host,
            f"test ! -e {shlex.quote(descriptor.remote_workspace)}",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")

    @pytest.mark.parametrize("mapped", (False, True), ids=("staged", "mapped"))
    def test_remote_openocd_config_consumes_forwarded_environment(self, mapped: bool) -> None:
        """Verify allow-listed environment reaches remote OpenOCD Tcl config."""
        openocd_command = self.ssh_settings.openocd_command
        executable = openocd_command[0]
        executable_result = self.ssh.run(
            self.host, f"test -x {shlex.quote(executable)}", timeout=20
        )
        if executable_result.returncode:
            pytest.skip(f"remote OpenOCD is not executable: {executable}")
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            config = Path(directory) / "environment config.cfg"
            config.write_text(
                "set zro_forwarded_value $::env(ZRO_CONFIG_VALUE)\n"
                "echo ZRO_CONFIG_VALUE=$zro_forwarded_value\n"
                "shutdown\n"
            )
            mappings: tuple[PathMapping, ...] = ()
            if mapped:
                remote_directory = PurePosixPath(deploy_helper(self.ssh, self.host).path).parent / (
                    "qualification mapped " + uuid.uuid4().hex
                )

                def remove_mapping() -> None:
                    result = self.ssh.run(
                        self.host,
                        "rm -rf " + shlex.quote(str(remote_directory)),
                        timeout=20,
                    )
                    assert result.returncode == 0, result.stderr.decode(errors="replace")

                resources.enter_context(cleanup_on_exit(remove_mapping))
                allocated = self.ssh.run(
                    self.host,
                    "mkdir -m 700 " + shlex.quote(str(remote_directory)),
                    timeout=20,
                )
                assert allocated.returncode == 0, allocated.stderr.decode(errors="replace")
                copied = self.ssh.run(
                    self.host,
                    "cat > " + shlex.quote(str(remote_directory / config.name)),
                    input_data=config.read_bytes(),
                    timeout=20,
                )
                assert copied.returncode == 0, copied.stderr.decode(errors="replace")
                mappings = (PathMapping(Path(directory), remote_directory),)
            planner = PathPlanner(mappings)
            planned = planner.plan_file(config, "environment")
            output = []
            process = ProcessSpec(
                (*openocd_command, "-f", planned.remote),
                (("ZRO_CONFIG_VALUE", "channel_1"),),
                required_paths=tuple(planner.remote_checks),
                argv_templates=(
                    ((len(openocd_command) + 1, planned.template),)
                    if planned.template is not None
                    else ()
                ),
                completion_policy=CompletionPolicy.PROCESS_EXIT,
            )
            request = SessionPlan(
                self.host,
                self.ssh,
                process,
                tuple(planner.staged_files),
                (),
            )
            session = SessionController.prepare(request)
            try:
                session.acquire(
                    output_handler=lambda stream, payload, line_end: output.append(
                        (stream, payload, line_end)
                    )
                )
                assert session.wait_for_openocd_exit(timeout=30) == 0
            finally:
                session.close()
            assert any(
                payload == "ZRO_CONFIG_VALUE=channel_1" for _stream, payload, _line_end in output
            )
