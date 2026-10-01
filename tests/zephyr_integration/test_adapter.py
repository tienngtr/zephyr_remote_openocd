# SPDX-License-Identifier: Apache-2.0

"""Real Zephyr parser/runner contracts without a board, SDK, or build invocation."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import socket
import subprocess
import threading
from pathlib import PurePosixPath
from unittest.mock import Mock, create_autospec

import pytest
from zephyr_remote_openocd.config import ConfigError, PathMapping, ResolvedRemote
from zephyr_remote_openocd.remote import RemoteSession
from zephyr_remote_openocd.remote.debug import DebugInputs, DebugPlan, build_debug_plan
from zephyr_remote_openocd.remote.forwarding import ForwardStartError
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
)
from zephyr_remote_openocd.remote.paths import PathPlanner
from zephyr_remote_openocd.remote.ssh import SshCommand

from tests.forwarding_support import GDB, RTT, TCL, TELNET, ForwardingHarness
from tests.support import env_path

pytestmark = pytest.mark.zephyr

TEST_PROCESS = RemoteProcess(("test-process",))
OPENOCD_FAILURE_RC = 7


def _debug_plan(
    *,
    gdb_argv: tuple[str, ...] | None = None,
    services: tuple[Service, ...] = (),
    rtt_service: Service | None = None,
) -> DebugPlan:
    return DebugPlan(
        RemoteProcess(("openocd",)),
        (),
        services,
        gdb_argv,
        False,
        None,
        False,
        rtt_service,
        None,
        rtt_service is not None,
    )


@pytest.fixture
def runner_api(monkeypatch):
    zephyr = env_path("ZEPHYR_BASE")
    if zephyr is None or not zephyr.is_dir():
        pytest.skip("ZEPHYR_BASE must name a Zephyr 4.4 source tree")
    pytest.importorskip("west")
    monkeypatch.syspath_prepend(str(zephyr / "scripts" / "west_commands"))
    # These modules become importable only after adding the selected Zephyr tree.
    import runners.core as core  # pylint: disable=no-name-in-module
    import runners.openocd as openocd  # pylint: disable=no-name-in-module
    from zephyr_remote_openocd.zephyr44.runner import RemoteOpenOcdBinaryRunner

    return core, openocd.OpenOcdBinaryRunner, RemoteOpenOcdBinaryRunner


def test_runner_output_reconstructs_fragment_boundaries(runner_api, monkeypatch):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(runner_module.sys, "stdout", stdout)
    monkeypatch.setattr(runner_module.sys, "stderr", stderr)

    runner_module._write_output("stdout", "long ", False)
    runner_module._write_output("stdout", "line", True)
    runner_module._write_output("stderr", "unterminated", False)

    assert stdout.getvalue() == "long line\n"
    assert stderr.getvalue() == "unterminated"


def test_remote_home_json_preserves_spaces(runner_api, monkeypatch, tmp_path):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    selected = ResolvedRemote(
        "lab",
        tmp_path / "config.yaml",
        "host",
        ("~/tools/open ocd",),
        ("ssh",),
        (),
        (PathMapping(tmp_path, PurePosixPath("~/remote tree")),),
    )
    monkeypatch.setattr(
        SshCommand,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps("/home/ Remote User ").encode() + b"\n", b""
            )
        ),
    )
    resolved = runner_module._prepare_remote_paths(selected)
    assert resolved.openocd_command == ("/home/ Remote User /tools/open ocd",)
    assert str(resolved.path_mappings[0].remote) == "/home/ Remote User /remote tree"


def test_remote_home_resolution_reports_ssh_failure(runner_api, monkeypatch, tmp_path):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    selected = ResolvedRemote(
        "lab",
        tmp_path / "config.yaml",
        "host",
        ("~/openocd",),
        ("ssh",),
        (),
        (),
    )
    monkeypatch.setattr(
        SshCommand,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                [], 255, b"", b"Permission denied (publickey)."
            )
        ),
    )

    with pytest.raises(ConfigError) as error:
        runner_module._prepare_remote_paths(selected)

    message = str(error.value)
    assert "remote home" in message
    assert "lab" in message
    assert "Permission denied" in message


@pytest.mark.parametrize(
    "output", (b'"relative"\n', b"null\n", b"not-json\n", b'"/bad\\u0000path"')
)
def test_remote_home_json_rejects_invalid_paths(runner_api, monkeypatch, tmp_path, output):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    selected = ResolvedRemote(
        "lab",
        tmp_path / "config.yaml",
        "host",
        ("~/openocd",),
        ("ssh",),
        (),
        (),
    )
    monkeypatch.setattr(
        SshCommand,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, output, b"")),
    )
    with pytest.raises(ConfigError, match="remote home query returned an invalid path"):
        runner_module._prepare_remote_paths(selected)


def test_gdb_execution_reports_session_status(runner_api):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    runner = Mock()
    session = Mock()
    session.check_openocd_exit.return_value = OPENOCD_FAILURE_RC
    plan = _debug_plan(gdb_argv=("gdb", "zephyr.elf"))

    returncode = runner_module._execute_gdb_client(runner, plan, session)

    runner.require.assert_called_once_with("gdb")
    runner.run_client.assert_called_once_with(["gdb", "zephyr.elf"])
    session.check_openocd_exit.assert_called_once_with()
    session.close.assert_not_called()
    assert returncode == OPENOCD_FAILURE_RC


def test_operation_build_queries_version_without_session(runner_api, monkeypatch, tmp_path):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    runner = Mock()
    runner.thread_info_enabled = True
    selected = ResolvedRemote(
        "lab",
        tmp_path / "config.yaml",
        "host",
        ("openocd", "-f", "board.cfg"),
        ("ssh", "-F", "config"),
        (),
        (),
    )
    query = Mock(return_value="Open On-Chip Debugger 0.12.0")
    request = Mock()
    plan = Mock()
    versions = []

    def capture_plan(_runner, command, _selected, version):
        assert command == "debug"
        versions.append(version)
        return plan

    monkeypatch.setattr(runner_module, "query_remote_openocd_version", query)
    monkeypatch.setattr(runner_module, "_debug_plan", capture_plan)
    monkeypatch.setattr(runner_module, "_debug_request", lambda *_args: request)
    monkeypatch.setattr(
        runner_module.RemoteSession,
        "open",
        Mock(side_effect=AssertionError("operation construction opened a session")),
    )

    result = runner_module._build_operation(runner, "debug", selected)

    assert result == (selected, request, plan)
    query.assert_called_once_with(
        SshCommand(selected.ssh_command),
        selected.remote_host,
        selected.openocd_command,
    )
    assert len(versions) == 1


@pytest.mark.parametrize(
    (
        "observed_returncode",
        "operation_fails",
        "cleanup_failure",
        "cleanup_returncode",
        "primary",
        "diagnostics",
    ),
    (
        pytest.param(None, False, None, None, None, (), id="success-cleanup-success"),
        pytest.param(
            None,
            False,
            None,
            OPENOCD_FAILURE_RC,
            "openocd",
            (),
            id="cleanup-discovers-openocd-failure",
        ),
        pytest.param(
            None,
            False,
            "cleanup failed",
            None,
            "cleanup",
            (),
            id="cleanup-failure-is-primary",
        ),
        pytest.param(
            None,
            False,
            "cleanup failed",
            OPENOCD_FAILURE_RC,
            "cleanup",
            ("remote OpenOCD also exited",),
            id="cleanup-failure-precedes-new-openocd-failure",
        ),
        pytest.param(
            None,
            True,
            None,
            None,
            "operation",
            (),
            id="operation-failure-cleanup-success",
        ),
        pytest.param(
            None,
            True,
            None,
            OPENOCD_FAILURE_RC,
            "operation",
            ("remote OpenOCD also exited",),
            id="operation-failure-precedes-new-openocd-failure",
        ),
        pytest.param(
            None,
            True,
            "cleanup failed",
            None,
            "operation",
            (
                "session cleanup also failed",
                "cleanup failed",
                "nested cleanup detail",
            ),
            id="operation-failure-precedes-cleanup-failure",
        ),
        pytest.param(
            OPENOCD_FAILURE_RC,
            False,
            "cleanup failed",
            OPENOCD_FAILURE_RC,
            "openocd",
            ("session cleanup also failed", "cleanup failed"),
            id="observed-openocd-failure-precedes-cleanup-failure",
        ),
        pytest.param(
            OPENOCD_FAILURE_RC,
            False,
            "helper failed",
            OPENOCD_FAILURE_RC,
            "openocd",
            ("session cleanup also failed", "helper failed"),
            id="observed-openocd-failure-precedes-later-infrastructure-failure",
        ),
    ),
)
def test_operation_primary_failure_rules(
    runner_api,
    monkeypatch,
    observed_returncode,
    operation_fails,
    cleanup_failure,
    cleanup_returncode,
    primary,
    diagnostics,
):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    runner = Mock()
    session = Mock()
    session.descriptor = SessionDescriptor(SessionAllocation("session", "/workspace"), "127.0.0.1")
    session.openocd_returncode = None
    operation_error = RuntimeError("operation failed")
    cleanup_error = RuntimeError(cleanup_failure) if cleanup_failure is not None else None
    if operation_fails and cleanup_error is not None:
        cleanup_error.add_note("nested cleanup detail")

    def execute_started_operation(*_args):
        if operation_fails:
            raise operation_error
        session.openocd_returncode = observed_returncode
        return observed_returncode

    def close():
        session.openocd_returncode = cleanup_returncode
        if cleanup_error is not None:
            raise cleanup_error

    session.close.side_effect = close
    monkeypatch.setattr(
        runner_module.RemoteSession,
        "open",
        create_autospec(RemoteSession.open, return_value=session),
    )
    monkeypatch.setattr(runner_module, "_execute_started_operation", execute_started_operation)
    request = RemoteSessionRequest("host", SshCommand(), TEST_PROCESS)

    if primary is None:
        runner_module._execute_operation(runner, "debug", request, None)
    else:
        with pytest.raises(RuntimeError) as raised:
            runner_module._execute_operation(runner, "debug", request, None)
        if primary == "operation":
            assert raised.value is operation_error
        elif primary == "cleanup":
            assert raised.value is cleanup_error
        else:
            assert str(OPENOCD_FAILURE_RC) in str(raised.value)
        notes = getattr(raised.value, "__notes__", ())
        if diagnostics:
            assert all(any(fragment in note for note in notes) for fragment in diagnostics)
        else:
            assert not notes

    session.close.assert_called_once_with()


@pytest.mark.parametrize(
    "reader_records_before_operation_failure",
    (True, False),
    ids=("reader-first", "active-operation-first"),
)
def test_background_openocd_result_does_not_replace_active_operation_failure(
    runner_api,
    monkeypatch,
    reader_records_before_operation_failure,
):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    reader_ready = threading.Event()
    reader_can_record = threading.Event()
    reader_recorded = threading.Event()
    operation_error = RuntimeError("active operation failed")

    class Session:
        descriptor = SessionDescriptor(SessionAllocation("session", "/workspace"), "127.64.0.1")

        def __init__(self):
            self.openocd_returncode = None
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            reader_can_record.set()
            assert reader_recorded.wait(5)

    session = Session()

    def record_openocd_failure():
        reader_ready.set()
        assert reader_can_record.wait(5)
        session.openocd_returncode = OPENOCD_FAILURE_RC
        reader_recorded.set()

    reader = threading.Thread(target=record_openocd_failure)
    reader.start()
    assert reader_ready.wait(5)

    def fail_operation(*_args):
        if reader_records_before_operation_failure:
            reader_can_record.set()
            assert reader_recorded.wait(5)
        raise operation_error

    monkeypatch.setattr(
        runner_module.RemoteSession,
        "open",
        create_autospec(RemoteSession.open, return_value=session),
    )
    monkeypatch.setattr(runner_module, "_execute_started_operation", fail_operation)
    request = RemoteSessionRequest("host", SshCommand(), TEST_PROCESS)

    try:
        with pytest.raises(RuntimeError) as raised:
            runner_module._execute_operation(Mock(), "debug", request, None)
    finally:
        reader_can_record.set()
        assert reader_recorded.wait(5)
        reader.join(timeout=5)

    assert raised.value is operation_error
    notes = raised.value.__notes__
    assert any(str(OPENOCD_FAILURE_RC) in note for note in notes)
    assert all("remote OpenOCD also exited" in note for note in notes)
    assert session.close_calls == 1
    assert not reader.is_alive()


def test_rtt_execution_defers_forward_until_after_gdb(runner_api, monkeypatch):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    session = harness.open()
    harness.ssh.process(RTT)
    runner = Mock()
    plan = _debug_plan(gdb_argv=("gdb", "--batch"), services=(GDB, TCL, TELNET), rtt_service=RTT)
    observed_returncodes: list[int] = []

    def batch_gdb(_argv):
        assert RTT not in session.forwarded_services
        harness.ssh.process(GDB).returncode = 13

    runner.run_client.side_effect = batch_gdb

    def run_rtt(_port, poll):
        assert RTT in session.forwarded_services
        assert poll() is None
        return 0

    client = Mock(side_effect=run_rtt)
    monkeypatch.setattr(runner_module, "run_rtt_client", client)

    try:
        returncode = runner_module._execute_rtt(runner, plan, session, observed_returncodes.append)
        assert [advisory.service for advisory in harness.advisories] == [GDB]
        assert observed_returncodes == []
        assert returncode == 0
        assert not session.closed
    finally:
        session.close()


def _forwarding_operation(runner_api, monkeypatch, tmp_path, command, harness, rtt_server=False):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    plan = build_debug_plan(
        DebugInputs(
            command,
            "openocd",
            "gdb",
            str(tmp_path / "app.elf"),
            (),
            (),
            "init-sentinel",
            "complete-sentinel",
            rtt_address=0x20000000,
            rtt_server=rtt_server,
        ),
        PathPlanner(()),
    )
    selected = ResolvedRemote(
        "chosen",
        tmp_path / "config.yaml",
        "host",
        ("openocd",),
        harness.ssh.argv_prefix,
        (),
        (),
    )
    monkeypatch.setattr(runner_module, "SshCommand", lambda _prefix: harness.ssh)
    runner = create_autospec(runner_api[1], instance=True)
    runner.logger = logging.getLogger("test.remote_openocd.forwarding")
    for service in (*plan.services, *((plan.rtt_service,) if plan.rtt_service else ())):
        harness.ssh.process(service)
    return runner, runner_module._debug_request(runner, selected, plan), plan


@pytest.mark.parametrize("command", ("debug", "attach", "debugserver"))
def test_required_gdb_startup_failure_aborts_operation(runner_api, monkeypatch, tmp_path, command):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner, request, plan = _forwarding_operation(
        runner_api, monkeypatch, tmp_path, command, harness
    )
    harness.ssh.process(GDB).ready = False
    with pytest.raises(ForwardStartError):
        runner_module._execute_operation(runner, command, request, plan)
    runner.run_client.assert_not_called()
    assert harness.helper.close_calls == 1


@pytest.mark.parametrize("command", ("debug", "debugserver"))
@pytest.mark.parametrize("phase", ("startup", "runtime"))
def test_optional_rtt_failure_preserves_gdb_operation(
    runner_api, monkeypatch, tmp_path, caplog, command, phase
):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner, request, plan = _forwarding_operation(
        runner_api,
        monkeypatch,
        tmp_path,
        command,
        harness,
        rtt_server=True,
    )
    if phase == "startup":
        harness.ssh.process(RTT).ready = False

    def exit_after_health_observation():
        harness.helper.openocd_returncode = 0

    def fail_best_effort_during_active_operation():
        assert harness.ssh.process(GDB).returncode is None
        harness.ssh.process(RTT).returncode = 13
        harness.helper.on_wait = exit_after_health_observation

    harness.helper.on_wait = fail_best_effort_during_active_operation
    runner.run_client.side_effect = lambda _argv: fail_best_effort_during_active_operation()
    with caplog.at_level(logging.INFO, logger=runner.logger.name):
        runner_module._execute_operation(runner, command, request, plan)
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "rtt" in warnings[0].getMessage().lower()
    assert str(RTT.local_port) in warnings[0].getMessage()
    if phase == "startup":
        assert not any("RTT server available" in record.getMessage() for record in caplog.records)
    if command == "debug":
        runner.run_client.assert_called_once()
    assert harness.helper.close_calls == 1
    assert harness.ssh.process(GDB).mock.close_stderr.call_count == 1


@pytest.mark.parametrize("failure", ("batch-gdb", "rtt-forward"))
def test_standalone_rtt_setup_failure_never_launches_client(
    runner_api, monkeypatch, tmp_path, failure
):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner, request, plan = _forwarding_operation(runner_api, monkeypatch, tmp_path, "rtt", harness)
    if failure == "batch-gdb":
        runner.run_client.side_effect = subprocess.CalledProcessError(9, ("gdb",))
    else:
        harness.ssh.process(RTT).ready = False
    client = Mock(side_effect=AssertionError("RTT client cannot start after setup failure"))
    monkeypatch.setattr(runner_module, "run_rtt_client", client)
    with pytest.raises((subprocess.CalledProcessError, ForwardStartError)):
        runner_module._execute_operation(runner, "rtt", request, plan)
    client.assert_not_called()
    assert harness.helper.close_calls == 1


def test_rtt_cleanup_failure_does_not_replace_observed_openocd_failure(runner_api, monkeypatch):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    runner = Mock()
    session = Mock(spec=RemoteSession)
    session.descriptor = SessionDescriptor(SessionAllocation("session", "/workspace"), "127.0.0.1")
    session.openocd_returncode = OPENOCD_FAILURE_RC
    session.check_openocd_exit.return_value = OPENOCD_FAILURE_RC
    rtt_service = Service("rtt", 19021, 19021)
    session.forwarded_services = (rtt_service,)
    plan = _debug_plan(gdb_argv=("gdb", "--batch"), services=(GDB,), rtt_service=rtt_service)
    rtt_cleanup_error = RuntimeError("RTT connection cleanup failed")

    def run_rtt(_port, poll):
        try:
            return poll()
        finally:
            raise rtt_cleanup_error

    monkeypatch.setattr(
        runner_module.RemoteSession,
        "open",
        create_autospec(RemoteSession.open, return_value=session),
    )
    monkeypatch.setattr(runner_module, "run_rtt_client", run_rtt)
    request = RemoteSessionRequest("host", SshCommand(), TEST_PROCESS)

    with pytest.raises(RuntimeError, match=str(OPENOCD_FAILURE_RC)) as raised:
        runner_module._execute_operation(runner, "rtt", request, plan)

    assert raised.value is not rtt_cleanup_error
    assert any("RTT connection cleanup failed" in note for note in raised.value.__notes__)
    session.check_openocd_exit.assert_called_once_with()
    session.close.assert_called_once_with()


def test_debugserver_execution_reports_gdb_service_and_waits(runner_api):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    runner = Mock()
    session = Mock()
    session.wait_for_openocd_exit.return_value = OPENOCD_FAILURE_RC
    gdb_service = Service("gdb", 3333, 3333)
    plan = _debug_plan(services=(gdb_service,))

    returncode = runner_module._execute_server(runner, "debugserver", plan, session)

    runner.logger.info.assert_called_once()
    assert 3333 in runner.logger.info.call_args.args
    session.wait_for_openocd_exit.assert_called_once_with()
    assert returncode == OPENOCD_FAILURE_RC


def parser_for(runner):
    parser = argparse.ArgumentParser(allow_abbrev=False)
    runner.add_parser(parser)
    return parser


@pytest.mark.parametrize(
    "argv",
    (
        [],
        ["--serial=probe", "--config=first.cfg", "--config=second.cfg"],
        ["--cmd-load=load", "--cmd-verify=verify", "--verify", "--erase"],
        [
            "--cmd-pre-init=one",
            "--cmd-pre-init=two",
            "--cmd-reset-halt=halt",
            "--cmd-pre-load=prepare",
            "--cmd-erase=erase",
            "--cmd-post-verify=done",
        ],
        [
            "--gdb-port=3334",
            "--gdb-client-port=3335",
            "--tcl-port=0",
            "--telnet-port=4445",
            "--gdb-init=halt",
            "--gdb-init=quit",
            "--no-load",
            "--tui",
        ],
        ["--rtt-port=19021", "--rtt-server", "--rtt-address=0x20000000"],
        [
            "--file=firmware.bin",
            "--file-type=bin",
            "--flash-address=0x20000000",
            "--cmd-load=flash write_image",
            "--no-init",
            "--no-halt",
            "--no-targets",
            "--target-handle=target",
        ],
    ),
)
def test_parser_preserves_applicable_upstream_options(runner_api, argv):
    _, upstream, remote = runner_api
    expected = vars(parser_for(upstream).parse_args(argv))
    actual = vars(parser_for(remote).parse_args([*argv, "--remote=chosen"]))
    assert actual.pop("remote") == "chosen"
    # Additional remote-only options are allowed; upstream options retain their meaning.
    assert {name: actual[name] for name in expected} == expected


def test_attach_rejects_rtt_server_before_remote_work(runner_api, tmp_path, monkeypatch):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    core, _, remote = runner_api
    build = tmp_path / "build"
    (build / "zephyr").mkdir(parents=True)
    (build / "zephyr" / ".config").write_text("# CONFIG_DEBUG_THREAD_INFO is not set\n")
    image = build / "zephyr" / "zephyr.elf"
    image.write_bytes(b"test image")
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_remote: chosen\n"
        "remotes:\n"
        "  chosen:\n"
        "    ssh_host: selected_host\n"
        "    openocd_command: [openocd]\n"
        "    ssh_command: [ssh]\n"
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    monkeypatch.delenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", raising=False)

    cfg = core.RunnerConfig(
        build_dir=str(build),
        board_dir=str(tmp_path),
        elf_file=str(image),
        exe_file=None,
        hex_file=None,
        bin_file=str(image),
        uf2_file=None,
        mot_file=None,
        file=None,
        file_type=core.FileType.BIN,
        gdb="gdb",
        openocd="openocd",
        openocd_search=[],
    )
    args = parser_for(remote).parse_args(["--rtt-server"])
    runner = remote.create(cfg, args)

    monkeypatch.setattr(SshCommand, "run", Mock(side_effect=AssertionError("SSH action started")))
    monkeypatch.setattr(
        runner_module.RemoteSession,
        "open",
        Mock(side_effect=AssertionError("remote session started")),
    )

    with pytest.raises(RuntimeError) as raised:
        runner.run("attach")

    message = str(raised.value)
    assert "attach" in message
    assert "--rtt-server" in message


@pytest.fixture
def forbid_external_io(monkeypatch):
    guards = []
    for owner, name in (
        (subprocess, "Popen"),
        (os, "system"),
        (socket, "socket"),
        (SshCommand, "run"),
        (SshCommand, "popen"),
        (SshCommand, "run_stream"),
    ):
        guard = Mock(side_effect=AssertionError(f"recording attempted external I/O: {name}"))
        monkeypatch.setattr(owner, name, guard)
        guards.append(guard)
    yield
    for guard in guards:
        guard.assert_not_called()  # Also catch accidentally suppressed I/O failures.


@pytest.mark.parametrize(
    "command", ("flash", "debug", "attach", "debugserver", "rtt", "debug-rtt", "debugserver-rtt")
)
@pytest.mark.parametrize("thread_info", (False, True))
def test_recording_runs_real_adapter_without_external_io(
    runner_api,
    tmp_path,
    monkeypatch,
    capsys,
    forbid_external_io,
    command,
    thread_info,
):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    rtt_server = command.endswith("-rtt")
    command = command.removesuffix("-rtt")
    core, _, remote = runner_api
    build = tmp_path / "build"
    (build / "zephyr").mkdir(parents=True)
    (build / "zephyr" / ".config").write_text(
        "CONFIG_DEBUG_THREAD_INFO=y\n" if thread_info else "# CONFIG_DEBUG_THREAD_INFO is not set\n"
    )
    image = build / "zephyr" / "zephyr.elf"
    image.write_bytes(b"test image; RTT address is explicitly supplied")
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_remote: unused\nremotes:\n"
        "  unused: {openocd_command: [openocd]}\n"
        "  chosen:\n    ssh_host: selected_host\n"
        "    openocd_command: ['~/tools/openocd', '--debug']\n"
        "    forward_env: [ZRO_CONTROLLED_ENV]\n"
        "    path_mappings: {'/': '~/mapped'}\n"
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "unused")
    monkeypatch.setenv("ZRO_CONTROLLED_ENV", "secret-value")
    monkeypatch.setenv("ZRO_RECORD", "1")
    if thread_info:
        monkeypatch.setenv("ZRO_RECORD_OPENOCD_VERSION", "Open On-Chip Debugger 0.12.0")
    cfg = core.RunnerConfig(
        build_dir=str(build),
        board_dir=str(tmp_path),
        elf_file=str(image),
        exe_file=None,
        hex_file=None,
        bin_file=str(image),
        uf2_file=None,
        mot_file=None,
        file=None,
        file_type=core.FileType.BIN,
        gdb="gdb",
        openocd="openocd",
        openocd_search=[str(tmp_path)],
        rtt_address=0x20000000,
    )
    args = parser_for(remote).parse_args(
        [
            "--remote=chosen",
            "--serial=probe",
            "--cmd-pre-init=echo test",
            "--gdb-init=monitor halt",
            "--rtt-port=19021",
            "--flash-address=0x20000000",
            "--cmd-load=flash write_image",
            *(["--rtt-server"] if rtt_server else []),
        ]
    )
    runner = remote.create(cfg, args)

    planned_requests = []
    original_record_operation = runner_module._record_operation

    def capture_record_operation(record_runner, record_command, selected):
        recorded = original_record_operation(record_runner, record_command, selected)
        planned_requests.append(recorded[0])
        return recorded

    monkeypatch.setattr(runner_module, "_record_operation", capture_record_operation)
    runner.run(command)
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["command"] == command
    request = result["remote_session_request"]
    assert request["host"] == "selected_host"
    assert request["process"]["argv"][:2] == ["~/tools/openocd", "--debug"]
    assert "echo test" in request["process"]["argv"]
    assert any("probe" in argument for argument in request["process"]["argv"])
    assert any("~/mapped/" in argument for argument in request["process"]["argv"])
    assert request["process"]["environment"] == ["ZRO_CONTROLLED_ENV"]
    assert "secret-value" not in output
    assert planned_requests[0] is not None
    assert dict(planned_requests[0].process.environment) == {"ZRO_CONTROLLED_ENV": "secret-value"}
    if command == "flash":
        assert request["services"] == []
    else:
        services = {item["name"]: item for item in request["services"]}
        assert services == {
            "gdb": {
                "name": "gdb",
                "local_port": 3333,
                "remote_port": 3333,
                "criticality": "required",
            },
            "tcl": {
                "name": "tcl",
                "local_port": 6333,
                "remote_port": 6333,
                "criticality": "auxiliary",
            },
            "telnet": {
                "name": "telnet",
                "local_port": 4444,
                "remote_port": 4444,
                "criticality": "auxiliary",
            },
            **(
                {
                    "rtt": {
                        "name": "rtt",
                        "local_port": 19021,
                        "remote_port": 19021,
                        "criticality": "auxiliary",
                    }
                }
                if rtt_server
                else {}
            ),
        }
        assert result["thread_info"]["requested"] is thread_info
        assert result["thread_info"]["version_source"] == ("injected" if thread_info else None)
    if command == "rtt":
        assert result["rtt"]["port"] == 19021
