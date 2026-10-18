# SPDX-License-Identifier: Apache-2.0

"""Real Zephyr parser/runner contracts without a board, SDK, or build invocation."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import TYPE_CHECKING, Any, BinaryIO, Literal, override
from unittest.mock import Mock, NonCallableMagicMock, create_autospec

import pytest
import yaml
from zephyr_remote_openocd.config import ConfigError, PathMapping, ResolvedRemote
from zephyr_remote_openocd.controller import session as session_module
from zephyr_remote_openocd.controller._deployment import DeploymentResult
from zephyr_remote_openocd.controller._forwarding import ForwardStartError
from zephyr_remote_openocd.controller._rtt import ObservedSession
from zephyr_remote_openocd.controller._ssh import ManagedSshProcess, SshForwardSpec
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionAllocation,
    SessionController,
    SessionError,
    SessionPlan,
    SshCommand,
)
from zephyr_remote_openocd.controller.cancellation import OperationCancelled, operation_scope
from zephyr_remote_openocd.controller.paths import PathPlanner
from zephyr_remote_openocd.openocd.debug import (
    DebugInputs,
    DebugPlan,
    OpenOcdVersion,
    build_debug_plan,
)
from zephyr_remote_openocd.openocd.flash import FlashPlanError
from zephyr_remote_openocd.protocol.arguments import ArgumentTemplate, SessionValue
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.outcome import Trigger
from zephyr_remote_openocd.protocol.process import ProcessSpec

from tests.concurrency_support import ConcurrentSessions
from tests.elf_fixtures import elf_memory_witness_bytes
from tests.forwarding_support import GDB, RTT, TCL, TELNET, ForwardingHarness
from tests.process_support import cleanup_on_exit, local_transport
from tests.protocol_support import terminal_snapshot
from tests.support import ROOT, env_path

if TYPE_CHECKING:
    # Resolvable only with the selected Zephyr tree on MYPYPATH.
    from runners.core import RunnerConfig, ZephyrBinaryRunner
    from runners.openocd import OpenOcdBinaryRunner
    from zephyr_remote_openocd.zephyr44.runner import RemoteOpenOcdBinaryRunner

# runners.core is imported from the selected Zephyr tree at fixture setup.
type RunnerApi = tuple[ModuleType, type[OpenOcdBinaryRunner], type[RemoteOpenOcdBinaryRunner]]

pytestmark = pytest.mark.zephyr

TEST_PROCESS = ProcessSpec(("test-process",))
OPENOCD_FAILURE_RC = 7


def _debug_plan(
    *,
    gdb_argv: tuple[str, ...] | None = None,
    forwards: tuple[ForwardSpec, ...] = (),
    rtt_forward: ForwardSpec | None = None,
) -> DebugPlan:
    return DebugPlan(
        ProcessSpec(("openocd",)),
        (),
        forwards,
        gdb_argv,
        False,
        None,
        False,
        rtt_forward,
        None,
        rtt_forward is not None,
    )


def _batch_gdb(
    monkeypatch: pytest.MonkeyPatch,
    runner_module: ModuleType,
    effect: Callable[[list[str]], int | None] | None = None,
) -> Mock:
    """Replace only RTT setup's batch GDB process execution, its effect boundary."""

    def run(
        argv: list[str], *, capture_output: bool = True, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        # Batch setup output stays on the user's terminal, as with check_call().
        assert not capture_output
        returncode = effect(argv) if effect is not None else None
        return subprocess.CompletedProcess(argv, returncode or 0)

    batch = Mock(side_effect=run)
    monkeypatch.setattr(runner_module, "run_finite", batch)
    return batch


@pytest.fixture
def runner_api(monkeypatch: pytest.MonkeyPatch) -> RunnerApi:
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


@pytest.fixture
def runner_config(runner_api: RunnerApi, tmp_path: Path) -> RunnerConfig:
    """Minimal build metadata for normal upstream runner initialization."""
    core, _, _ = runner_api
    build = tmp_path / "build"
    (build / "zephyr").mkdir(parents=True)
    (build / "zephyr" / ".config").write_text("# CONFIG_DEBUG_THREAD_INFO is not set\n")
    return core.RunnerConfig(
        build_dir=str(build),
        board_dir=str(tmp_path),
        elf_file=None,
        exe_file=None,
        hex_file=None,
        bin_file=None,
        uf2_file=None,
        mot_file=None,
        file=None,
        gdb="gdb",
        openocd="openocd",
        openocd_search=[],
    )


@pytest.fixture
def firmware(runner_config: RunnerConfig) -> Path:
    """An explicit no-build artifact; these tests never parse its ELF contents."""
    image = Path(runner_config.build_dir) / "zephyr" / "zephyr.elf"
    image.write_bytes(b"test image")
    return image


def test_runner_output_reconstructs_fragment_boundaries(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(runner_module.sys, "stdout", stdout)
    monkeypatch.setattr(runner_module.sys, "stderr", stderr)

    runner_module.write_output("stdout", "long ", False)
    runner_module.write_output("stdout", "line", True)
    runner_module.write_output("stderr", "unterminated", False)

    assert stdout.getvalue() == "long line\n"
    assert stderr.getvalue() == "unterminated"


@pytest.mark.parametrize("level", (logging.INFO, logging.DEBUG), ids=("normal", "verbose"))
@pytest.mark.parametrize("forward_ready", (True, False), ids=("success", "forward-failure"))
def test_runner_logs_effective_remote_argv(
    runner_api: RunnerApi,
    runner_config: RunnerConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    level: int,
    forward_ready: bool,
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    _, _, remote = runner_api
    harness = ForwardingHarness(monkeypatch)
    harness.helper.allocation = SessionAllocation("session", "/workspace with spaces")
    harness.helper.on_wait = lambda: harness.helper.authority.record_terminal(
        terminal_snapshot(Trigger.CHILD_EXIT, returncode=0)
    )
    for forward in (GDB, TCL, TELNET):
        harness.ssh.process(forward)
    harness.ssh.process(GDB).ready = forward_ready
    monkeypatch.setattr(runner_module, "SshCommand", lambda _prefix: harness.ssh)
    fixed = ["/tools/open ocd", "-c", "init", "-f", "fixed {workspace} {address}.cfg", ""]
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "default_remote": "chosen",
                "remotes": {"chosen": {"openocd_command": fixed}},
            }
        )
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    runner = remote.create(
        runner_config,
        parser_for(remote).parse_args(["--cmd-pre-init=echo {workspace}/generated.cfg"]),
    )
    processes: list[ProcessSpec] = []
    start_process = harness.helper.start_process

    def capture_start(
        process: ProcessSpec,
        forwards: Iterable[ListenerSpec],
        *,
        preferred_address: str | None = None,
    ) -> str:
        processes.append(process)
        return start_process(process, forwards, preferred_address=preferred_address)

    monkeypatch.setattr(harness.helper, "start_process", capture_start)
    with caplog.at_level(level, logger=runner.logger.name):
        if forward_ready:
            runner.run("debugserver")
        else:
            with pytest.raises(ForwardStartError):
                runner.run("debugserver")

    assert processes[0].argv[: len(fixed)] == tuple(fixed)
    assert processes[0].literal_prefix == len(fixed)
    commands = [
        record.getMessage().partition(": ")[2]
        for record in caplog.records
        if record.getMessage().startswith("Remote OpenOCD: ")
    ]
    if level == logging.DEBUG:
        assert len(commands) == 1
        argv = shlex.split(commands[0])
        assert argv[: len(fixed)] == fixed
        assert "bindto 127.64.0.1" in argv
        assert "echo {workspace}/generated.cfg" in argv
        assert argv == fixed + [
            "bindto 127.64.0.1" if arg == "bindto {address}" else arg
            for arg in processes[0].argv[len(fixed) :]
        ]
    else:
        assert commands == []
    assert harness.helper.close_calls == 1


@pytest.mark.parametrize("failure", ("spawn", "probe-busy"))
def test_runner_logs_effective_argv_when_remote_startup_fails(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    helper = ROOT / "python/zephyr_remote_openocd/helper/__main__.py"
    environment = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)}

    class LocalHelperCommand(SshCommand):
        @override
        def popen(
            self,
            host: str,
            remote_command: str,
            *,
            local_forward: SshForwardSpec | None = None,
            on_acquired: Callable[[ManagedSshProcess], None] | None = None,
        ) -> ManagedSshProcess:
            del host, remote_command
            assert local_forward is None
            return local_transport(
                [sys.executable, str(helper), "control"],
                environment=environment,
                on_acquired=on_acquired,
            )

        @override
        def run_stream(
            self, host: str, remote_command: str, input_stream: BinaryIO, *, timeout: float = 60
        ) -> subprocess.CompletedProcess[bytes]:
            del host
            return subprocess.run(
                [sys.executable, str(helper), "stage", shlex.split(remote_command)[-1]],
                env=environment,
                input=input_stream.read(),
                capture_output=True,
                timeout=timeout,
                check=False,
            )

        @override
        def run(
            self,
            host: str,
            remote_command: str,
            *,
            input_data: bytes | None = None,
            timeout: float = 15,
        ) -> subprocess.CompletedProcess[bytes]:
            raise AssertionError("run() is not expected")

    monkeypatch.setattr(
        session_module,
        "deploy_helper",
        lambda *_args: DeploymentResult(str(helper), "test", False),
    )
    fixed = (
        (str(tmp_path / "missing-openocd"), "-c", "init")
        if failure == "spawn"
        else (
            sys.executable,
            "-c",
            "import sys; print('requested probe is busy', file=sys.stderr); "
            f"sys.exit({OPENOCD_FAILURE_RC})",
        )
    )
    request = SessionPlan(
        "local",
        LocalHelperCommand(),
        ProcessSpec(
            (*fixed, "{workspace}/generated file", "{address}"),
            required_output_sentinels=("missing-startup-marker",),
            literal_prefix=len(fixed),
            argv_templates=(
                (len(fixed), ArgumentTemplate((SessionValue.WORKSPACE, "/generated file"))),
                (len(fixed) + 1, ArgumentTemplate((SessionValue.ADDRESS,))),
            ),
        ),
    )
    runner = create_autospec(runner_api[2], instance=True)
    runner.logger = logging.getLogger("test.remote_openocd.startup")
    with caplog.at_level(logging.DEBUG, logger=runner.logger.name), pytest.raises(SessionError):
        runner_module.execute_operation(runner, "debugserver", request, None)

    commands = [
        shlex.split(record.getMessage().partition(": ")[2])
        for record in caplog.records
        if record.getMessage().startswith("Remote OpenOCD: ")
    ]
    assert len(commands) == 1
    assert commands[0][: len(fixed)] == list(fixed)
    assert commands[0][-2].startswith(str(tmp_path / "zephyr_remote_openocd"))
    assert commands[0][-2].endswith("/generated file")
    assert commands[0][-1].startswith("127.")
    assert not list((tmp_path / "zephyr_remote_openocd").glob("*/staged"))


def test_remote_home_json_preserves_spaces(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

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
    resolved = runner_module.prepare_remote_paths(selected)
    assert resolved.openocd_command == ("/home/ Remote User /tools/open ocd",)
    assert str(resolved.path_mappings[0].remote) == "/home/ Remote User /remote tree"


def test_remote_home_resolution_reports_ssh_failure(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

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
        runner_module.prepare_remote_paths(selected)

    message = str(error.value)
    assert "remote home" in message
    assert "lab" in message
    assert "Permission denied" in message


@pytest.mark.parametrize(
    "output", (b'"relative"\n', b"null\n", b"not-json\n", b'"/bad\\u0000path"')
)
def test_remote_home_json_rejects_invalid_paths(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: bytes
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

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
    with pytest.raises(ConfigError):
        runner_module.prepare_remote_paths(selected)


def test_interactive_gdb_owns_ctrl_c_until_runner_capture_is_restored(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    harness.helper.on_wait = lambda: None
    runner = Mock()
    plan = _debug_plan(gdb_argv=("gdb", "zephyr.elf"), forwards=(GDB,))
    dispositions = []

    def interactive_gdb(_argv: list[str]) -> None:
        dispositions.append(signal.getsignal(signal.SIGINT))
        # Terminal Ctrl-C during the client belongs to GDB, not the runner.
        signal.raise_signal(signal.SIGINT)

    runner.run_client.side_effect = interactive_gdb
    with pytest.raises(OperationCancelled), operation_scope() as operation:
        session = harness.prepare((GDB,), ())
        with cleanup_on_exit(session.close):
            session.acquire()
            assert runner_module.execute_started_operation(runner, "debug", plan, session) is None
            assert not operation.requested
            # Runner capture is restored before any further runner work.
            signal.raise_signal(signal.SIGINT)
            assert operation.requested
    assert dispositions == [signal.SIG_IGN]


def test_cancellation_latched_before_gdb_entry_refuses_launch(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner = Mock()
    plan = _debug_plan(gdb_argv=("gdb", "zephyr.elf"), forwards=(GDB,))
    with pytest.raises(OperationCancelled) as raised, operation_scope() as operation:
        session = harness.prepare((GDB,), ())
        with cleanup_on_exit(session.close):
            session.acquire()
            signal.raise_signal(signal.SIGINT)
            runner_module.execute_gdb_client(runner, plan, session)
    runner.run_client.assert_not_called()
    assert raised.value is operation.cancellation
    assert harness.helper.authority.snapshot().failure is operation.cancellation


def test_rtt_batch_gdb_is_cancellable_runner_work(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner = Mock()
    plan = _debug_plan(gdb_argv=("gdb", "--batch"), forwards=(GDB,), rtt_forward=RTT)
    dispositions = []

    def batch_gdb(_argv: list[str]) -> None:
        dispositions.append(signal.getsignal(signal.SIGINT))
        # Ctrl-C during setup requests runner cancellation.
        signal.raise_signal(signal.SIGINT)

    batch = _batch_gdb(monkeypatch, runner_module, batch_gdb)
    client = Mock(side_effect=AssertionError("RTT client cannot start after cancellation"))
    monkeypatch.setattr(runner_module, "run_rtt_client", client)
    with pytest.raises(OperationCancelled), operation_scope() as operation:
        session = harness.prepare((GDB,), ())
        with cleanup_on_exit(session.close):
            session.acquire()
            harness.ssh.process(RTT)
            runner_module.execute_rtt(runner, plan, session)
    batch.assert_called_once()
    # The setup interval never became an interactive client interval.
    assert dispositions and dispositions[0] not in (signal.SIG_IGN, signal.SIG_DFL)
    assert operation.requested
    # Cancellation is admitted before the RTT forward and client effects.
    assert RTT not in session.forwarded_specs
    client.assert_not_called()


@pytest.mark.parametrize(
    "command, terminal",
    (
        ("debug", 0),
        ("debug", OPENOCD_FAILURE_RC),
        ("debug", "error"),
        ("attach", OPENOCD_FAILURE_RC),
        ("rtt", OPENOCD_FAILURE_RC),
    ),
)
def test_recorded_termination_before_gdb_dispatch_prevents_client_launch(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    terminal: int | Literal["error"],
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    session = harness.prepare()
    try:
        session.acquire()
        runner = Mock()
        plan = _debug_plan(gdb_argv=("gdb", "firmware.elf"), rtt_forward=RTT)
        failure = SessionError("helper failed before client dispatch")

        def terminate_before_dispatch(_executable: str) -> None:
            if terminal == "error":
                monkeypatch.setattr(harness.helper, "observe_status", Mock(side_effect=failure))
            else:
                harness.helper.authority.record_terminal(
                    terminal_snapshot(Trigger.CHILD_EXIT, returncode=terminal)
                )

        runner.require.side_effect = terminate_before_dispatch
        rtt_client = Mock()
        monkeypatch.setattr(runner_module, "run_rtt_client", rtt_client)
        batch = _batch_gdb(monkeypatch, runner_module)
        with pytest.raises((RuntimeError, SessionError)):
            runner_module.execute_started_operation(runner, command, plan, session)
        runner.run_client.assert_not_called()
        batch.assert_not_called()
        rtt_client.assert_not_called()
    finally:
        session.close()


@pytest.mark.parametrize("terminal", (0, OPENOCD_FAILURE_RC, "error"))
def test_recorded_termination_during_rtt_forward_prevents_rtt_client_launch(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, terminal: int | Literal["error"]
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    request = harness.request()
    rtt_forward = harness.ssh.process(RTT)
    runner = Mock()
    plan = _debug_plan(gdb_argv=("gdb", "--batch"), forwards=(GDB,), rtt_forward=RTT)
    ready = harness.await_ready
    failure = SessionError("helper failed during RTT forwarding")

    def terminate_during_forward(
        process: ManagedSshProcess, sentinel: str, deadline: float
    ) -> bool:
        if process is rtt_forward.managed:
            if terminal == "error":
                monkeypatch.setattr(harness.helper, "observe_status", Mock(side_effect=failure))
            else:
                harness.helper.authority.record_terminal(
                    terminal_snapshot(Trigger.CHILD_EXIT, returncode=terminal)
                )
        return ready(process, sentinel, deadline)

    monkeypatch.setattr(
        "zephyr_remote_openocd.controller._forwarding.await_ready",
        terminate_during_forward,
    )
    client = Mock()
    monkeypatch.setattr(runner_module, "run_rtt_client", client)
    batch = _batch_gdb(monkeypatch, runner_module)
    with pytest.raises(RuntimeError) as raised:
        runner_module.execute_operation(runner, "rtt", request, plan)
    if terminal == "error":
        assert raised.value is failure
    elif terminal == OPENOCD_FAILURE_RC:
        assert str(OPENOCD_FAILURE_RC) in str(raised.value)
        # Child failure is published directly; RTT startup never becomes eligible.
    batch.assert_called_once()
    client.assert_not_called()
    assert harness.helper.close_calls == 1
    assert all(process.returncode is not None for process in harness.ssh.processes.values())


def test_operation_build_queries_version_without_session(
    runner_api: RunnerApi,
    runner_config: RunnerConfig,
    firmware: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    _, _, remote = runner_api
    build = Path(runner_config.build_dir)
    (build / "zephyr" / ".config").write_text("CONFIG_DEBUG_THREAD_INFO=y\n")
    cfg = runner_config._replace(elf_file=str(firmware))
    runner = remote.create(cfg, parser_for(remote).parse_args([]))
    # Upstream declares create() as returning the ZephyrBinaryRunner base.
    assert isinstance(runner, remote)
    selected = ResolvedRemote(
        "lab",
        tmp_path / "config.yaml",
        "host",
        ("openocd",),
        ("configured-ssh", "-F", "config"),
        (),
        (),
    )
    query = create_autospec(
        runner_module.query_remote_openocd_version,
        return_value="Open On-Chip Debugger 0.12.0",
    )
    monkeypatch.setattr(runner_module, "query_remote_openocd_version", query)

    def forbid_prepare(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("operation planning must not prepare a session")

    monkeypatch.setattr(SessionController, "prepare", forbid_prepare)
    resolved, request, plan = runner_module.build_operation(runner, "debug", selected)
    assert resolved == selected
    assert plan is not None
    assert plan.thread_info_requested
    assert plan.openocd_version == OpenOcdVersion(0, 12, 0, "Open On-Chip Debugger 0.12.0")
    assert plan.rtos_awareness
    assert "$_TARGETNAME configure -rtos Zephyr" in plan.process.argv
    assert request.process == plan.process
    assert request.forwards == plan.forwards
    assert request.host == selected.remote_host
    assert request.ssh_command == SshCommand(selected.ssh_command)
    query.assert_called_once_with(
        SshCommand(selected.ssh_command),
        selected.remote_host,
        selected.openocd_command,
    )


@pytest.mark.parametrize(
    (
        "earlier_openocd_failure",
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
            ("exit status 7",),
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
            ("exit status 7",),
            id="operation-failure-precedes-new-openocd-failure",
        ),
        pytest.param(
            None,
            True,
            "cleanup failed",
            None,
            "operation",
            (
                "later session failure",
                "cleanup failed",
                "nested cleanup detail",
            ),
            id="operation-failure-precedes-cleanup-failure",
        ),
        pytest.param(
            "recognized",
            True,
            None,
            None,
            "openocd",
            ("operation failed",),
            id="recognized-openocd-failure-precedes-operation-failure",
        ),
        pytest.param(
            "observed",
            False,
            "cleanup failed",
            OPENOCD_FAILURE_RC,
            "openocd",
            ("later session failure", "cleanup failed"),
            id="observed-openocd-failure-precedes-cleanup-failure",
        ),
    ),
)
def test_operation_primary_failure_rules(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    earlier_openocd_failure: str | None,
    operation_fails: bool,
    cleanup_failure: str | None,
    cleanup_returncode: int | None,
    primary: str | None,
    diagnostics: tuple[str, ...],
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    session = harness.prepare()
    operation_error = RuntimeError("operation failed")
    cleanup_error = RuntimeError(cleanup_failure) if cleanup_failure is not None else None
    if cleanup_error is not None:
        if operation_fails:
            cleanup_error.add_note("nested cleanup detail")
        harness.ssh.process(GDB).cleanup_error = cleanup_error

    def execute_started_operation(*_args: object) -> None:
        if earlier_openocd_failure is not None:
            # A reader-recognized failure precedes the operation failure even
            # when the coordinator has not yet observed it.
            harness.helper.authority.record_terminal(
                terminal_snapshot(Trigger.CHILD_EXIT, returncode=OPENOCD_FAILURE_RC)
            )
            if earlier_openocd_failure == "observed":
                session.poll_operation()
        if operation_fails:
            raise operation_error

    close_helper = harness.helper.close

    def close() -> None:
        close_helper()
        if cleanup_returncode is not None and session.openocd_returncode is None:
            harness.helper.authority.record_terminal(
                terminal_snapshot(Trigger.CHILD_EXIT, returncode=cleanup_returncode)
            )

    monkeypatch.setattr(harness.helper, "close", close)
    monkeypatch.setattr(
        runner_module.SessionController, "prepare", lambda *_args, **_kwargs: session
    )
    monkeypatch.setattr(runner_module, "execute_started_operation", execute_started_operation)

    if primary is None:
        runner_module.execute_operation(Mock(), "debug", session.plan, None)
    else:
        with pytest.raises(RuntimeError) as raised:
            runner_module.execute_operation(Mock(), "debug", session.plan, None)
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
    assert session.closed
    assert harness.helper.close_calls == 1


@pytest.mark.parametrize(
    "command, phase",
    (
        ("flash", "staging"),
        ("flash", "ready"),
        ("debugserver", "staging"),
        ("debugserver", "ready"),
    ),
)
def test_interruption_during_session_acquisition_closes_owned_resources(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, command: str, phase: str
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    forwards = () if command == "flash" else (GDB,)
    request = harness.request(forwards=forwards, auxiliary=())
    runner = create_autospec(runner_api[1], instance=True)
    runner.logger = logging.getLogger("test.remote_openocd.interruption")
    interruption = KeyboardInterrupt()
    acquired: SessionController | None = None
    original_acquire = SessionController.acquire

    def acquire_and_interrupt(
        session: SessionController,
        *,
        output_handler: Callable[[str, str, bool], None] | None = None,
        process_start_handler: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        nonlocal acquired
        acquired = session
        original_acquire(
            session, output_handler=output_handler, process_start_handler=process_start_handler
        )
        raise interruption

    monkeypatch.setattr(SessionController, "acquire", acquire_and_interrupt)
    if phase == "staging":

        def interrupt_staging(*_args: object, **_kwargs: object) -> None:
            raise interruption

        monkeypatch.setattr(harness.ssh, "run_stream", interrupt_staging)
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            runner_module.execute_operation(runner, command, request, None)

        assert raised.value is interruption
        assert acquired is not None
        assert acquired.closed
        assert not acquired.forwarded_specs
        assert harness.helper.close_calls == 1
        runner.run_client.assert_not_called()
        for forward in forwards if phase == "ready" else ():
            process = harness.ssh.process(forward)
            assert process.returncode is not None
            stdin = process.managed.stdin
            assert stdin is not None and stdin.closed
    finally:
        if acquired is not None:
            acquired.close()


def test_rtt_execution_defers_forward_until_after_gdb(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    session = harness.prepare()
    try:
        session.acquire()
        harness.ssh.process(RTT)
        runner = Mock()
        plan = _debug_plan(
            gdb_argv=("gdb", "--batch"), forwards=(GDB, TCL, TELNET), rtt_forward=RTT
        )

        def batch_gdb(_argv: list[str]) -> None:
            assert RTT not in session.forwarded_specs
            harness.ssh.process(GDB).returncode = 13

        _batch_gdb(monkeypatch, runner_module, batch_gdb)

        def run_rtt(_port: int, observed: ObservedSession) -> int | None:
            assert RTT in session.forwarded_specs
            assert observed.poll_operation() is None
            return 0

        client = Mock(side_effect=run_rtt)
        monkeypatch.setattr(runner_module, "run_rtt_client", client)

        returncode = runner_module.execute_rtt(runner, plan, session)
        assert [advisory.forward for advisory in harness.advisories] == [GDB]
        assert returncode == 0
        assert not session.closed
    finally:
        session.close()


def _forwarding_operation(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    harness: ForwardingHarness,
    rtt_server: bool = False,
) -> tuple[NonCallableMagicMock, SessionPlan, DebugPlan]:
    """Control client dispatch while exercising real forwarding and cleanup."""
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

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
    for forward in (*plan.forwards, *((plan.rtt_forward,) if plan.rtt_forward else ())):
        harness.ssh.process(forward)
    return runner, runner_module.debug_request(runner, selected, plan), plan


@pytest.mark.parametrize("command", ("debug", "attach", "debugserver"))
def test_required_gdb_startup_failure_aborts_operation(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner, request, plan = _forwarding_operation(
        runner_api, monkeypatch, tmp_path, command, harness
    )
    harness.ssh.process(GDB).ready = False
    with pytest.raises(ForwardStartError):
        runner_module.execute_operation(runner, command, request, plan)
    runner.run_client.assert_not_called()
    assert harness.helper.close_calls == 1


def test_best_effort_startup_failure_warns_with_service_and_port(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner, request, plan = _forwarding_operation(
        runner_api, monkeypatch, tmp_path, "debug", harness
    )
    harness.ssh.process(TCL).ready = False

    def client_with_required_forwarding(_argv: list[str]) -> None:
        assert harness.ssh.process(GDB).returncode is None
        harness.helper.authority.record_terminal(
            terminal_snapshot(Trigger.CHILD_EXIT, returncode=0)
        )

    runner.run_client.side_effect = client_with_required_forwarding
    with caplog.at_level(logging.INFO, logger=runner.logger.name):
        runner_module.execute_operation(runner, "debug", request, plan)

    runner.run_client.assert_called_once()
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert any(
        TCL.name in message and f"127.0.0.1:{TCL.local_port}" in message for message in warnings
    )
    assert harness.helper.close_calls == 1


@pytest.mark.parametrize("command", ("debug", "attach", "debugserver"))
@pytest.mark.parametrize("phase", ("startup", "runtime"))
def test_requested_rtt_failure_aborts_operation(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    command: str,
    phase: str,
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

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

    def exit_after_health_observation() -> None:
        harness.helper.authority.record_terminal(
            terminal_snapshot(Trigger.CHILD_EXIT, returncode=0)
        )

    def fail_rtt_during_active_operation() -> None:
        assert harness.ssh.process(GDB).returncode is None
        harness.ssh.process(RTT).returncode = 13
        harness.helper.on_wait = exit_after_health_observation

    harness.helper.on_wait = fail_rtt_during_active_operation
    runner.run_client.side_effect = lambda _argv: fail_rtt_during_active_operation()
    expected_error = ForwardStartError if phase == "startup" else SessionError
    with (
        caplog.at_level(logging.INFO, logger=runner.logger.name),
        pytest.raises(expected_error),
    ):
        runner_module.execute_operation(runner, command, request, plan)
    assert not any(record.levelno == logging.WARNING for record in caplog.records)
    if phase == "startup":
        runner.run_client.assert_not_called()
    elif command in {"debug", "attach"}:
        runner.run_client.assert_called_once()
    assert harness.helper.close_calls == 1
    for forward in (GDB, RTT):
        assert harness.ssh.process(forward).mock.close_stderr.call_count == 1


@pytest.mark.parametrize("failure", ("batch-gdb", "rtt-forward"))
def test_standalone_rtt_setup_failure_never_launches_client(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner, request, plan = _forwarding_operation(runner_api, monkeypatch, tmp_path, "rtt", harness)
    _batch_gdb(monkeypatch, runner_module, (lambda _argv: 9) if failure == "batch-gdb" else None)
    if failure == "rtt-forward":
        harness.ssh.process(RTT).ready = False
    client = Mock(side_effect=AssertionError("RTT client cannot start after setup failure"))
    monkeypatch.setattr(runner_module, "run_rtt_client", client)
    with pytest.raises((subprocess.CalledProcessError, ForwardStartError)):
        runner_module.execute_operation(runner, "rtt", request, plan)
    client.assert_not_called()
    assert harness.helper.close_calls == 1


def test_rtt_cleanup_failure_does_not_replace_observed_openocd_failure(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    runner = Mock()
    harness = ForwardingHarness(monkeypatch)
    rtt_forward = ForwardSpec("rtt", 19021, 19021)
    harness.ssh.process(rtt_forward)
    plan = _debug_plan(gdb_argv=("gdb", "--batch"), forwards=(GDB,), rtt_forward=rtt_forward)
    rtt_cleanup_error = RuntimeError("RTT connection cleanup failed")

    def run_rtt(_port: int, observed: ObservedSession) -> int | None:
        harness.helper.authority.record_terminal(
            terminal_snapshot(Trigger.CHILD_EXIT, returncode=OPENOCD_FAILURE_RC)
        )
        try:
            return observed.poll_operation()
        finally:
            raise rtt_cleanup_error

    monkeypatch.setattr(runner_module, "run_rtt_client", run_rtt)
    _batch_gdb(monkeypatch, runner_module)
    request = harness.request(forwards=(GDB,), auxiliary=())

    with pytest.raises(RuntimeError, match=str(OPENOCD_FAILURE_RC)) as raised:
        runner_module.execute_operation(runner, "rtt", request, plan)

    assert raised.value is not rtt_cleanup_error
    assert any("RTT connection cleanup failed" in note for note in raised.value.__notes__)
    assert harness.helper.close_calls == 1
    assert all(process.returncode is not None for process in harness.ssh.processes.values())


@pytest.mark.parametrize("returncode", (0, OPENOCD_FAILURE_RC))
def test_debugserver_execution_reports_gdb_service_and_waits(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    returncode: int,
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner, request, plan = _forwarding_operation(
        runner_api, monkeypatch, tmp_path, "debugserver", harness
    )
    harness.helper.on_wait = lambda: harness.helper.authority.record_terminal(
        terminal_snapshot(Trigger.CHILD_EXIT, returncode=returncode)
    )
    session = SessionController.prepare(request)
    with cleanup_on_exit(session.close):
        session.acquire()
        with caplog.at_level(logging.INFO, logger=runner.logger.name):
            if returncode == 0:
                assert runner_module.execute_server(runner, "debugserver", plan, session) == 0
            else:
                with pytest.raises(SessionError) as raised:
                    runner_module.execute_server(runner, "debugserver", plan, session)
                assert raised.value is harness.helper.authority.snapshot().failure
                terminal = harness.helper.authority.snapshot().terminal
                assert terminal is not None and terminal.outcome.child_result is not None
                assert terminal.outcome.child_result.returncode == returncode
    assert any(
        "GDB" in record.getMessage() and f"127.0.0.1:{GDB.local_port}" in record.getMessage()
        for record in caplog.records
    )
    runner.run_client.assert_not_called()
    assert harness.helper.close_calls == 1


def parser_for(runner: type[OpenOcdBinaryRunner]) -> argparse.ArgumentParser:
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
def test_parser_preserves_applicable_upstream_options(
    runner_api: RunnerApi, argv: list[str]
) -> None:
    _, upstream, remote = runner_api
    expected = vars(parser_for(upstream).parse_args(argv))
    actual = vars(parser_for(remote).parse_args([*argv, "--remote=chosen"]))
    assert actual.pop("remote") == "chosen"
    # Additional remote-only options are allowed; upstream options retain their meaning.
    assert {name: actual[name] for name in expected} == expected


@pytest.mark.parametrize("child_returncode", (0, OPENOCD_FAILURE_RC))
def test_attach_rtt_server_forwards_without_loading(
    runner_api: RunnerApi,
    runner_config: RunnerConfig,
    firmware: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    child_returncode: int,
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    core, _, remote = runner_api
    harness = ForwardingHarness(monkeypatch)
    rtt_forward = ForwardSpec("rtt", 19021, 19021)
    for forward in (GDB, TCL, TELNET, rtt_forward):
        harness.ssh.process(forward)
    monkeypatch.setattr(runner_module, "SshCommand", lambda _prefix: harness.ssh)
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

    cfg = runner_config._replace(
        elf_file=str(firmware),
        bin_file=str(firmware),
        file_type=core.FileType.BIN,
        rtt_address=0x20000000,
    )
    args = parser_for(remote).parse_args(["--rtt-server", "--rtt-port=19021"])
    runner = remote.create(cfg, args)

    client_argv: list[list[str]] = []

    def attach_client(argv: list[str]) -> None:
        client_argv.append(argv)
        assert set(harness.helper.listeners) == {
            forward.listener for forward in (GDB, TCL, TELNET, rtt_forward)
        }
        assert harness.ssh.process(GDB).returncode is None
        assert harness.ssh.process(rtt_forward).returncode is None
        harness.helper.authority.record_terminal(
            terminal_snapshot(Trigger.CHILD_EXIT, returncode=child_returncode)
        )

    monkeypatch.setattr(runner, "require", create_autospec(runner.require))
    monkeypatch.setattr(runner, "run_client", attach_client)
    monkeypatch.setattr(
        runner_module,
        "run_rtt_client",
        Mock(side_effect=AssertionError("attach must not launch an RTT client")),
    )
    with caplog.at_level(logging.INFO, logger=runner.logger.name):
        if child_returncode:
            with pytest.raises(SessionError) as raised:
                runner.run("attach")
            assert str(child_returncode) in str(raised.value)
        else:
            runner.run("attach")

    assert len(client_argv) == 1
    assert client_argv[0][0] == "gdb"
    assert "target extended-remote 127.0.0.1:3333" in client_argv[0]
    assert str(firmware) in client_argv[0]
    assert "load" not in client_argv[0]
    assert any(
        "RTT" in record.getMessage() and "127.0.0.1:19021" in record.getMessage()
        for record in caplog.records
    )
    assert harness.helper.close_calls == 1
    assert all(process.returncode == 0 for process in harness.ssh.processes.values())


@pytest.fixture
def forbid_external_io(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    guards: list[Mock] = []
    for owner, name in (
        # Owned launch subclasses bind the class itself, not the module name.
        (subprocess.Popen, "__init__"),
        (subprocess, "Popen"),
        (os, "system"),
        (socket, "socket"),
        (SshCommand, "run"),
        (SshCommand, "popen"),
        (SshCommand, "run_stream"),
        # Recording needs no native Ctrl-C capture for a live operation.
        (signal, "set_wakeup_fd"),
    ):
        guard = Mock(side_effect=AssertionError(f"recording attempted external I/O: {name}"))
        monkeypatch.setattr(owner, name, guard)
        guards.append(guard)
    yield
    for guard in guards:
        guard.assert_not_called()  # Also catch accidentally suppressed I/O failures.


@pytest.mark.parametrize(
    "command",
    (
        "flash",
        "debug",
        "attach",
        "debugserver",
        "rtt",
        "debug-rtt",
        "attach-rtt",
        "debugserver-rtt",
    ),
)
def test_recording_runs_real_runner_integration_without_external_io(
    runner_api: RunnerApi,
    runner_config: RunnerConfig,
    firmware: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    forbid_external_io: None,
    command: str,
) -> None:
    rtt_server = command.endswith("-rtt")
    command = command.removesuffix("-rtt")
    core, _, remote = runner_api
    build = Path(runner_config.build_dir)
    (tmp_path / "interface").mkdir()
    (tmp_path / "interface" / "example.cfg").write_text("# search-relative config\n")
    monkeypatch.chdir(build)
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_remote: unused\nremotes:\n"
        "  unused: {openocd_command: [openocd]}\n"
        "  chosen:\n    ssh_host: selected_host\n"
        "    openocd_command: ['~/tools/openocd', '--debug']\n"
        "    forward_env: [ZRO_CONTROLLED_ENV, ZRO_ABSENT_ENV]\n"
        "    path_mappings: {'/': '~/mapped'}\n"
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", "unused")
    monkeypatch.setenv("ZRO_CONTROLLED_ENV", "secret-value")
    monkeypatch.delenv("ZRO_ABSENT_ENV", raising=False)
    monkeypatch.setenv("ZRO_RECORD", "1")
    cfg = runner_config._replace(
        elf_file=str(firmware),
        bin_file=str(firmware),
        file_type=core.FileType.BIN,
        openocd_search=[str(tmp_path)],
        rtt_address=0x20000000,
    )
    args = parser_for(remote).parse_args(
        [
            "--remote=chosen",
            "--config=interface/example.cfg",
            "--serial=probe",
            "--cmd-pre-init=echo test",
            "--gdb-init=monitor halt",
            "--gdb-port=3333",
            "--gdb-client-port=3334",
            "--rtt-port=19021",
            "--flash-address=0x20000000",
            "--cmd-load=flash write_image",
            *(["--rtt-server"] if rtt_server else []),
        ]
    )
    runner = remote.create(cfg, args)

    runner.run(command)
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["command"] == command
    request = result["remote_session_request"]
    assert request["host"] == "selected_host"
    assert request["process"]["argv"][:2] == ["~/tools/openocd", "--debug"]
    argv = request["process"]["argv"]
    assert argv[argv.index("-f") + 1] == f"~/mapped{tmp_path}/interface/example.cfg"
    assert "echo test" in request["process"]["argv"]
    assert any("probe" in argument for argument in request["process"]["argv"])
    assert any("~/mapped/" in argument for argument in request["process"]["argv"])
    assert request["process"]["environment"] == ["ZRO_CONTROLLED_ENV"]
    assert "secret-value" not in output
    assert any(
        record.levelno == logging.WARNING and "ZRO_ABSENT_ENV" in record.getMessage()
        for record in caplog.records
    )
    if command == "flash":
        assert request["services"] == []
    else:
        gdb_endpoint = 3333 if command == "debugserver" else 3334
        forwards = {item["name"]: item for item in request["services"]}
        assert forwards == {
            "gdb": {
                "name": "gdb",
                "local_port": gdb_endpoint,
                "remote_port": gdb_endpoint,
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
                        "criticality": "required",
                    }
                }
                if rtt_server
                else {}
            ),
        }
        assert "gdb_port 3333" in argv
        if command == "debugserver":
            assert result["local_gdb_argv"] is None
            assert not request["reserved_services"]
        else:
            assert "target extended-remote 127.0.0.1:3334" in result["local_gdb_argv"]
            assert {item["remote_port"] for item in request["reserved_services"]} == (
                {3333, 19021} if command == "rtt" else {3333}
            )
            if command == "attach":
                assert "load" not in result["local_gdb_argv"]
        if rtt_server:
            assert "rtt server start 19021 0" in argv
    if command == "rtt":
        assert result["rtt"]["port"] == 19021


@pytest.fixture
def comparison_config(
    runner_api: RunnerApi,
    runner_config: RunnerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> RunnerConfig:
    """Inputs shared by the built-in runner and an identity-mapped remote runner."""
    core, _, _ = runner_api
    executables = tmp_path / "bin"
    executables.mkdir()
    for name in ("openocd", "gdb"):
        (executables / name).write_text("#!/bin/sh\n")
        (executables / name).chmod(0o755)
    (tmp_path / "support").mkdir()
    (tmp_path / "support" / "openocd.cfg").write_text("# board configuration\n")
    (tmp_path / "search").mkdir()
    images = Path(runner_config.build_dir) / "zephyr"
    (images / "zephyr.elf").write_bytes(elf_memory_witness_bytes())
    (images / "zephyr.hex").write_text(":00000001FF\n")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "default_remote": "chosen",
                "remotes": {
                    "chosen": {
                        "ssh_host": "selected_host",
                        "openocd_command": [str(executables / "openocd")],
                        # Remote paths equal local paths, so no path translation applies.
                        "path_mappings": {"/": "/"},
                    }
                },
            }
        )
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    monkeypatch.delenv("ZEPHYR_REMOTE_OPENOCD_REMOTE", raising=False)
    return runner_config._replace(
        elf_file=str(images / "zephyr.elf"),
        hex_file=str(images / "zephyr.hex"),
        gdb=str(executables / "gdb"),
        openocd=str(executables / "openocd"),
        openocd_search=[str(tmp_path / "search")],
        file_type=core.FileType.HEX,
    )


def _remove_runner_command(argv: list[str], command: str) -> list[str]:
    indices = [
        index for index in range(len(argv) - 1) if argv[index : index + 2] == ["-c", command]
    ]
    assert len(indices) == 1, f"expected one runner-owned command {command!r}: {argv}"
    return argv[: indices[0]] + argv[indices[0] + 2 :]


def _without_service_ports(argv: list[str]) -> tuple[list[str], list[str]]:
    """Separate runner-owned service-port commands, whose position is not contractual.

    The commands are returned sorted rather than collapsed, so a duplicate, missing,
    or malformed service-port command still fails the comparison.
    """
    rest: list[str] = []
    ports: list[str] = []
    index = 0
    while index < len(argv):
        command = argv[index + 1] if argv[index] == "-c" and index + 1 < len(argv) else ""
        if command.split(" ", 1)[0] in {"tcl_port", "telnet_port", "gdb_port"}:
            ports.append(command)
            index += 2
        else:
            rest.append(argv[index])
            index += 1
    return rest, sorted(ports)


def _built_in_commands(
    runner: ZephyrBinaryRunner, command: str, caplog: pytest.LogCaptureFixture
) -> list[list[str]]:
    """Commands logged by the built-in runner with its inherited dry_run flag set."""
    openocd, gdb = runner.cfg.openocd, runner.cfg.gdb
    assert openocd is not None and gdb is not None
    executables = (openocd, gdb)
    runner.dry_run = True
    with caplog.at_level(logging.INFO, logger=runner.logger.name):
        runner.run(command)
    commands = [
        shlex.split(record.getMessage())
        for record in caplog.records
        if record.name == runner.logger.name
        and record.getMessage().startswith(tuple(shlex.quote(item) + " " for item in executables))
    ]
    caplog.clear()
    # Zephyr passes some commands, such as init, targets, halt, and serial selection,
    # as one "-c <command>" argv element. OpenOCD reads the same command as from the
    # separate "-c" and "<command>" elements, so only that argv spelling is split.
    return [
        [
            part
            for argument in argv
            for part in (("-c", argument[3:]) if argument.startswith("-c ") else (argument,))
        ]
        for argv in commands
    ]


def _recorded_commands(recording: dict[str, Any]) -> list[list[str]]:
    request = recording["remote_session_request"]
    if request is None:
        return []
    process = request["process"]
    argv = _remove_runner_command(process["argv"], "bindto {address}")
    sentinels = process["required_output_sentinels"]
    if sentinels:
        initialized, complete = sentinels
        argv = _remove_runner_command(argv, f"lappend post_init_commands {{echo {initialized}}}")
        argv = _remove_runner_command(argv, f"echo {complete}")
    gdb = recording["local_gdb_argv"]
    return [argv, *([gdb] if gdb is not None else [])]


@pytest.mark.parametrize(
    "command, file_type, argv",
    (
        pytest.param(
            "flash",
            "HEX",
            [
                "--cmd-load=flash write_image erase",
                "--cmd-verify=verify_image",
                "--cmd-erase=flash erase_sector 0 0 last",
                "--erase",
                "--verify",
                "--cmd-pre-init=echo pre-init",
                "--cmd-pre-load=echo pre-load",
                "--cmd-post-verify=echo post-verify",
                "--serial=probe",
            ],
            id="hex-erase-verify",
        ),
        pytest.param(
            "flash",
            "ELF",
            ["--verify-only", "--cmd-pre-load=echo pre-load", "--cmd-post-verify=echo post-verify"],
            id="elf-verify-only",
        ),
        pytest.param(
            "debug",
            "ELF",
            ["--cmd-pre-init=echo pre-init", "--gdb-init=break main"],
            id="debug",
        ),
    ),
)
def test_commands_match_built_in_runner(
    runner_api: RunnerApi,
    comparison_config: RunnerConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    forbid_external_io: None,
    command: str,
    file_type: str,
    argv: list[str],
) -> None:
    """Detect unexpected differences from Zephyr's runner in matching scenarios.

    Only runner-owned remote differences are normalized. Matching does not make
    every built-in behavior desirable: deliberate differences
    (REQ-FUNC-OPT-008) stay out of these scenarios rather than being normalized
    away.
    """
    core, upstream, remote = runner_api
    cfg = comparison_config._replace(file_type=getattr(core.FileType, file_type))
    expected = _built_in_commands(
        upstream.create(cfg, parser_for(upstream).parse_args(argv)), command, caplog
    )
    monkeypatch.setenv("ZRO_RECORD", "1")
    remote.create(cfg, parser_for(remote).parse_args(argv)).run(command)
    actual = _recorded_commands(json.loads(capsys.readouterr().out))

    assert len(actual) == len(expected)
    if expected:
        expected_openocd, expected_ports = _without_service_ports(expected[0])
        actual_openocd, actual_ports = _without_service_ports(actual[0])
        # Runner-generated firmware paths are quoted as Tcl words remotely.
        image = getattr(cfg, f"{file_type.lower()}_file")
        actual_openocd = [item.replace(f'"{image}"', image) for item in actual_openocd]
        assert actual_openocd == expected_openocd
        assert actual_ports == expected_ports
        if "--serial=probe" not in argv:
            assert not any("_ZEPHYR_BOARD_SERIAL" in item for item in actual[0])
    if len(expected) == 2:
        # The local GDB client reaches the forwarded endpoint on IPv4 loopback.
        assert actual[1] == [
            item.replace("target extended-remote :", "target extended-remote 127.0.0.1:")
            for item in expected[1]
        ]


@pytest.mark.parametrize(
    ("file_type", "argv"),
    (
        pytest.param(
            "HEX",
            ["--cmd-load=flash write_image erase", "--cmd-verify=verify_image", "--erase"],
            id="erase-without-erase-command",
        ),
        pytest.param(
            "BIN",
            ["--cmd-load=flash write_image", "--flash-address=0x20000000", "--verify-only"],
            id="bin-verify-without-verify-command",
        ),
    ),
)
def test_live_flash_with_unperformable_request_fails_without_remote_work(
    runner_api: RunnerApi,
    comparison_config: RunnerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_type: str,
    argv: list[str],
) -> None:
    core, _, remote = runner_api
    for name in ("run", "popen", "run_stream"):
        monkeypatch.setattr(
            SshCommand,
            name,
            Mock(side_effect=AssertionError(f"SshCommand.{name}() is not expected in this test")),
        )
    # Remote-relative paths would otherwise require a remote home query.
    config = tmp_path / "home-relative.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "default_remote": "chosen",
                "remotes": {
                    "chosen": {
                        "ssh_host": "selected_host",
                        "openocd_command": ["~/bin/openocd"],
                        "path_mappings": {"/": "~/mirror"},
                    }
                },
            }
        )
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    assert comparison_config.hex_file is not None
    image = Path(comparison_config.hex_file).with_suffix(".bin")
    image.write_bytes(b"\x00")
    cfg = comparison_config._replace(
        file_type=getattr(core.FileType, file_type), bin_file=str(image)
    )
    runner = remote.create(cfg, parser_for(remote).parse_args(argv))

    # Deliberately stricter than Zephyr 4.4 (REQ-FUNC-OPT-008), which returns
    # success without performing the requested erase or verification.
    with pytest.raises(RuntimeError) as raised:
        runner.run("flash")
    assert isinstance(raised.value.__cause__, FlashPlanError)


def test_thread_info_recording_requires_injected_version_without_remote_query(
    runner_api: RunnerApi,
    runner_config: RunnerConfig,
    firmware: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forbid_external_io: None,
) -> None:
    _, _, remote = runner_api
    build = Path(runner_config.build_dir)
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_remote: chosen\nremotes:\n  chosen:\n    openocd_command: [openocd]\n"
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    monkeypatch.setenv("ZRO_RECORD", "1")
    cfg = runner_config._replace(elf_file=str(firmware))
    # Without thread information, an unusable injected version is never consulted.
    monkeypatch.setenv("ZRO_RECORD_OPENOCD_VERSION", "unused invalid version")
    remote.create(cfg, parser_for(remote).parse_args([])).run("debug")
    thread_info = json.loads(capsys.readouterr().out)["thread_info"]
    assert not thread_info["requested"]
    assert thread_info["version_source"] is None
    assert not thread_info["rtos_awareness"]

    (build / "zephyr/.config").write_text("CONFIG_DEBUG_THREAD_INFO=y\n")
    runner = remote.create(cfg, parser_for(remote).parse_args([]))
    # Establish a valid recording path before removing only its version input.
    monkeypatch.setenv("ZRO_RECORD_OPENOCD_VERSION", "Open On-Chip Debugger 0.12.0")
    runner.run("debug")
    thread_info = json.loads(capsys.readouterr().out)["thread_info"]
    assert thread_info["requested"]
    assert thread_info["version_source"] == "injected"
    assert thread_info["rtos_awareness"]

    monkeypatch.delenv("ZRO_RECORD_OPENOCD_VERSION")
    with pytest.raises(RuntimeError):
        runner.run("debug")
    assert capsys.readouterr().out == ""


def test_adapter_allows_same_probe_session_to_finish_while_another_is_active(
    runner_api: RunnerApi,
    runner_config: RunnerConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, remote = runner_api
    concurrent = ConcurrentSessions(tmp_path)
    opened = (threading.Event(), threading.Event())
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "default_remote": "chosen",
                "remotes": {
                    "chosen": {
                        "ssh_host": "test-host",
                        "ssh_command": list(concurrent.ssh_argv),
                        "openocd_command": list(concurrent.child_argv),
                    }
                },
            }
        )
    )
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(config))
    cfg = runner_config._replace(hex_file=str(concurrent.image))
    runners = tuple(
        remote.create(
            cfg,
            parser_for(remote).parse_args(
                [
                    "--serial=test-probe",
                    f"--cmd-pre-init=test_channel {channel}",
                    "--cmd-load=program",
                    "--cmd-verify=verify_image",
                ]
            ),
        )
        for channel in (0, 1)
    )
    original_acquire = SessionController.acquire

    def acquire(
        session: SessionController,
        *,
        output_handler: Callable[[str, str, bool], None] | None = None,
        process_start_handler: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        original_acquire(
            session, output_handler=output_handler, process_start_handler=process_start_handler
        )
        channel = next(
            int(argument.removeprefix("test_channel "))
            for argument in session.plan.process.argv
            if argument.startswith("test_channel ")
        )
        opened[channel].set()

    monkeypatch.setattr(SessionController, "acquire", acquire)
    concurrent.check_progress(lambda channel: runners[channel].run("flash"), opened=opened)


@pytest.mark.parametrize(
    ("command", "phase"),
    (("debug", "gdb"), ("rtt", "gdb"), ("rtt", "rtt"), ("debugserver", "wait")),
)
def test_active_operation_interrupt_closes_session_and_forwards(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, phase: str
) -> None:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    harness = ForwardingHarness(monkeypatch)
    runner, request, plan = _forwarding_operation(
        runner_api, monkeypatch, tmp_path, command, harness
    )
    interruption = KeyboardInterrupt()

    def interrupt(*_args: object) -> None:
        raise interruption

    if command == "rtt":
        # Standalone RTT setup runs batch GDB as ordinary process work.
        _batch_gdb(monkeypatch, runner_module, interrupt if phase == "gdb" else None)
    if phase == "gdb" and command != "rtt":
        runner.run_client.side_effect = interrupt
    elif phase == "rtt":
        monkeypatch.setattr(runner_module, "run_rtt_client", interrupt)
    elif phase == "wait":
        harness.helper.on_wait = interrupt
    with pytest.raises(KeyboardInterrupt) as raised:
        runner_module.execute_operation(runner, command, request, plan)
    assert raised.value is interruption
    assert harness.helper.close_calls == 1
    for forward in (*plan.forwards, *((plan.rtt_forward,) if phase == "rtt" else ())):
        process = harness.ssh.process(forward)
        assert process.returncode is not None
        stdin = process.managed.stdin
        assert stdin is not None and stdin.closed


def test_ctrl_c_during_live_preparation_projects_keyboard_interrupt(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zephyr_remote_openocd.controller.waiting import wait_for
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    previous = signal.getsignal(signal.SIGINT)
    execute = Mock(side_effect=AssertionError("no session after cancelled preparation"))

    def prepare(*_args: object) -> None:
        # Ctrl-C during finite remote preparation latches, then the next wait
        # dispatches it before any session work begins.
        signal.raise_signal(signal.SIGINT)
        wait_for()
        pytest.fail("cancellation was not dispatched")

    monkeypatch.setattr(runner_module, "build_operation", prepare)
    monkeypatch.setattr(runner_module, "execute_operation", execute)
    runner = Mock()
    with pytest.raises(KeyboardInterrupt) as raised:
        runner_module.run_live_operation(runner, "debug", Mock())
    assert raised.value.__cause__ is None
    execute.assert_not_called()
    assert signal.getsignal(signal.SIGINT) is previous


def _debugserver_operation(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: ForwardingHarness,
) -> tuple[ModuleType, NonCallableMagicMock]:
    from zephyr_remote_openocd.zephyr44 import _runner as runner_module

    runner, request, plan = _forwarding_operation(
        runner_api, monkeypatch, tmp_path, "debugserver", harness
    )
    monkeypatch.setattr(runner_module, "build_operation", lambda *_args: (None, request, plan))
    return runner_module, runner


def test_session_failure_before_ctrl_c_stays_primary(
    runner_api: RunnerApi, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = ForwardingHarness(monkeypatch)
    runner_module, runner = _debugserver_operation(runner_api, monkeypatch, tmp_path, harness)

    def fail_then_interrupt() -> None:
        # The helper reader records a failure, then Ctrl-C arrives before the
        # coordinator's next observation.
        harness.helper.authority.record_terminal(
            terminal_snapshot(Trigger.HELPER_FAILURE, code="EARLIER_HELPER_FAILURE")
        )
        signal.raise_signal(signal.SIGINT)

    harness.helper.on_wait = fail_then_interrupt
    with pytest.raises(SessionError) as raised:
        runner_module.run_live_operation(runner, "debugserver", None)
    assert "EARLIER_HELPER_FAILURE" in str(raised.value)
    assert any("Ctrl-C" in note for note in raised.value.__notes__)
    assert harness.helper.close_calls == 1


def test_ctrl_c_before_cleanup_failure_projects_interrupt_with_diagnostics(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = ForwardingHarness(monkeypatch)
    runner_module, runner = _debugserver_operation(runner_api, monkeypatch, tmp_path, harness)
    harness.helper.on_wait = lambda: signal.raise_signal(signal.SIGINT)
    harness.ssh.process(GDB).cleanup_error = OSError("gdb forward cleanup failed")
    with (
        caplog.at_level(logging.INFO, logger=runner.logger.name),
        pytest.raises(KeyboardInterrupt),
    ):
        runner_module.run_live_operation(runner, "debugserver", None)
    messages = [record.getMessage() for record in caplog.records]
    assert any("gdb forward cleanup failed" in message for message in messages)
    assert all(process.returncode is not None for process in harness.ssh.processes.values())
    assert harness.helper.close_calls == 1


CLEANUP_STATUS = "Interrupted; cleaning up the remote OpenOCD session"


def test_cleanup_status_is_reported_once_despite_repeated_ctrl_c(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = ForwardingHarness(monkeypatch)
    runner_module, runner = _debugserver_operation(runner_api, monkeypatch, tmp_path, harness)
    harness.helper.on_wait = lambda: signal.raise_signal(signal.SIGINT)
    for forward in (GDB, TCL):
        # Every independent transport cleanup sees another Ctrl-C.
        harness.ssh.process(forward).mock.close_stderr.side_effect = lambda: (
            signal.raise_signal(signal.SIGINT)
        )
    with (
        caplog.at_level(logging.INFO, logger=runner.logger.name),
        pytest.raises(KeyboardInterrupt),
    ):
        runner_module.run_live_operation(runner, "debugserver", None)
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count(CLEANUP_STATUS) == 1
    assert all(process.returncode is not None for process in harness.ssh.processes.values())
    assert all(
        process.mock.close_stderr.call_count == 1 for process in harness.ssh.processes.values()
    )
    assert harness.helper.close_calls == 1


def test_ctrl_c_after_the_last_wait_is_recognized_at_closure(
    runner_api: RunnerApi,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = ForwardingHarness(monkeypatch)
    runner_module, runner = _debugserver_operation(runner_api, monkeypatch, tmp_path, harness)
    exited = False

    def openocd_exits() -> None:
        nonlocal exited
        exited = True
        harness.helper.authority.record_terminal(
            terminal_snapshot(Trigger.CHILD_EXIT, returncode=0)
        )

    def observe_after_exit() -> None:
        # Ctrl-C arrives after OpenOCD's natural exit and the final wait.
        if exited:
            signal.raise_signal(signal.SIGINT)

    harness.helper.on_wait = openocd_exits
    monkeypatch.setattr(harness.helper, "observe_status", observe_after_exit)
    with (
        caplog.at_level(logging.INFO, logger=runner.logger.name),
        pytest.raises(KeyboardInterrupt),
    ):
        runner_module.run_live_operation(runner, "debugserver", None)
    messages = [record.getMessage() for record in caplog.records]
    assert messages.count(CLEANUP_STATUS) == 1
    assert harness.helper.close_calls == 1


@pytest.mark.parametrize(
    "reference, command",
    (
        ("board.cfg", "flash"),
        ("./board.cfg", "flash"),
        ("absolute", "flash"),
        # The debug-family commands share one planning path from the runner.
        ("board.cfg", "debug"),
    ),
)
def test_existing_cwd_config_reaches_real_initialization_and_recording(
    runner_api: RunnerApi,
    runner_config: RunnerConfig,
    firmware: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forbid_external_io: None,
    reference: str,
    command: str,
) -> None:
    core, _, remote = runner_api
    board = tmp_path / "board"
    support = board / "support"
    support.mkdir(parents=True)
    (support / "board.cfg").write_text("# search-directory alternative\n")
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    selected = invocation / "board.cfg"
    selected.write_text("# cwd selection\n")
    configuration = tmp_path / "config.yaml"
    configuration.write_text(
        "default_remote: chosen\n"
        "remotes:\n"
        "  chosen:\n"
        "    openocd_command: [openocd]\n"
        "    path_mappings: {'/': '/mapped'}\n"
    )
    monkeypatch.chdir(invocation)
    monkeypatch.setenv("ZEPHYR_REMOTE_OPENOCD_CONFIG", str(configuration))
    monkeypatch.setenv("ZRO_RECORD", "1")
    cfg = runner_config._replace(
        board_dir=str(board),
        elf_file=str(firmware),
        bin_file=str(firmware),
        file_type=core.FileType.BIN,
        openocd_search=[str(support)],
    )
    argument = str(selected) if reference == "absolute" else reference
    runner = remote.create(
        cfg,
        parser_for(remote).parse_args(
            [f"--config={argument}", "--cmd-load=flash write_image", "--flash-address=0x20000000"]
        ),
    )
    runner.run(command)
    request = json.loads(capsys.readouterr().out)["remote_session_request"]
    argv = request["process"]["argv"]
    assert argv[argv.index("-f") + 1] == f"/mapped{selected}"
    assert f"/mapped{support}" in argv
