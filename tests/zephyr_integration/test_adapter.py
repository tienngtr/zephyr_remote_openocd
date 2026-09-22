# SPDX-License-Identifier: Apache-2.0

"""Real Zephyr parser/runner contracts without a board, SDK, or build invocation."""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import subprocess
from pathlib import PurePosixPath
from unittest.mock import Mock

import pytest
from zephyr_remote_openocd.config import ConfigError, PathMapping, ResolvedRemote
from zephyr_remote_openocd.remote import RemoteSession
from zephyr_remote_openocd.remote.debug import DebugPlan
from zephyr_remote_openocd.remote.model import (
    RemoteProcess,
    RemoteSessionRequest,
    Service,
    SessionAllocation,
    SessionDescriptor,
)
from zephyr_remote_openocd.remote.ssh import SshCommand

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


@pytest.mark.parametrize(
    (
        "foreground_returncode",
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
                "session cleanup also failed: cleanup failed",
                "session cleanup also failed detail: nested cleanup detail",
            ),
            id="operation-failure-precedes-cleanup-failure",
        ),
        pytest.param(
            OPENOCD_FAILURE_RC,
            False,
            "cleanup failed",
            OPENOCD_FAILURE_RC,
            "openocd",
            ("session cleanup also failed: cleanup failed",),
            id="observed-openocd-failure-precedes-cleanup-failure",
        ),
        pytest.param(
            OPENOCD_FAILURE_RC,
            False,
            "helper failed",
            OPENOCD_FAILURE_RC,
            "openocd",
            ("session cleanup also failed: helper failed",),
            id="observed-openocd-failure-precedes-later-infrastructure-failure",
        ),
    ),
)
def test_operation_failure_precedence(
    runner_api,
    monkeypatch,
    foreground_returncode,
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
        session.openocd_returncode = foreground_returncode
        return foreground_returncode

    def close():
        session.openocd_returncode = cleanup_returncode
        if cleanup_error is not None:
            raise cleanup_error

    session.close.side_effect = close
    monkeypatch.setattr(runner_module.RemoteSession, "open", Mock(return_value=session))
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


def test_rtt_execution_defers_forward_until_after_gdb(runner_api, monkeypatch):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    calls = []
    runner = Mock()
    runner.run_client.side_effect = lambda _argv: calls.append("gdb")
    session = Mock(spec=RemoteSession)
    session.forward.side_effect = lambda _services: calls.append("forward")
    rtt_service = Service("rtt", 19021, 19021)
    plan = _debug_plan(gdb_argv=("gdb", "--batch"), rtt_service=rtt_service)

    def run_rtt(_port, _poll):
        calls.append("rtt")
        return 3

    client = Mock(side_effect=run_rtt)
    monkeypatch.setattr(runner_module, "run_rtt_client", client)

    returncode = runner_module._execute_rtt(runner, plan, session)

    assert calls == ["gdb", "forward", "rtt"]
    session.forward.assert_called_once_with((rtt_service,))
    client.assert_called_once_with(rtt_service.local_port, session.check_openocd_exit)
    session.close.assert_not_called()
    assert returncode == 3


def test_debugserver_execution_reports_gdb_service_and_waits(runner_api):
    from zephyr_remote_openocd.zephyr44 import runner as runner_module

    runner = Mock()
    session = Mock()
    session.wait_for_openocd_exit.return_value = 5
    gdb_service = Service("gdb", 3333, 3333)
    plan = _debug_plan(services=(gdb_service,))

    returncode = runner_module._execute_server(runner, "debugserver", plan, session)

    runner.logger.info.assert_called_once()
    assert 3333 in runner.logger.info.call_args.args
    session.wait_for_openocd_exit.assert_called_once_with()
    assert returncode == 5


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


@pytest.mark.parametrize("command", ("flash", "debug", "attach", "debugserver", "rtt"))
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
    if command != "flash":
        assert result["thread_info"]["requested"] is thread_info
        assert result["thread_info"]["version_source"] == ("injected" if thread_info else None)
    if command == "rtt":
        assert result["rtt"]["port"] == 19021
