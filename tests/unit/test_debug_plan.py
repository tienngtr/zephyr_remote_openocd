# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.controller.forwarding_spec import ForwardSpec
from zephyr_remote_openocd.controller.paths import (
    REMOTE_ADDRESS_PLACEHOLDER,
    PathPlanner,
)
from zephyr_remote_openocd.openocd.debug import (
    DebugInputs,
    DebugPlanError,
    build_debug_plan,
    parse_openocd_version,
    thread_info_enabled,
)
from zephyr_remote_openocd.protocol.listeners import ListenerSpec
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy


class TestDebugPlanning:
    def inputs(self, root, command="debug", **changes):
        config = root / "openocd.cfg"
        config.write_text("# config\n")
        values = dict(
            command=command,
            executable="/remote/openocd",
            gdb="/local/gdb",
            elf_file="/local/zephyr.elf",
            search_paths=(str(root),),
            config_files=(str(config),),
            openocd_init_sentinel="ZRO_OPENOCD_INIT_test",
            startup_complete_sentinel="ZRO_STARTUP_COMPLETE_test",
        )
        values.update(changes)
        return DebugInputs(**values)

    @pytest.mark.parametrize(
        ("missing", "message"),
        (("gdb", "GDB executable"), ("elf_file", "ELF file")),
    )
    def test_missing_client_metadata_fails_during_planning(
        self, tmp_path: Path, missing: str, message: str
    ):
        changes = {missing: None}
        planner = PathPlanner(())

        with pytest.raises(DebugPlanError) as error:
            build_debug_plan(
                self.inputs(tmp_path, search_paths=(), config_files=(), **changes),
                planner,
            )

        assert message.lower() in str(error.value).lower()
        assert not planner.staged_files
        assert not planner.remote_checks

    def test_command_semantics_and_client_ordering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            debug = build_debug_plan(
                self.inputs(
                    root,
                    gdb_init=("info registers", "quit"),
                    tui=True,
                ),
                PathPlanner(()),
            )
            assert debug.gdb_argv is not None
            assert "-tui" in debug.gdb_argv
            forwards = {item.name: item for item in debug.forwards}
            assert forwards == {
                "gdb": ForwardSpec("gdb", 3333, 3333),
                "tcl": ForwardSpec("tcl", 6333, 6333),
                "telnet": ForwardSpec("telnet", 4444, 4444),
            }
            assert debug.gdb_argv[-6:] == (
                "-ex",
                "load",
                "-ex",
                "info registers",
                "-ex",
                "quit",
            )
            assert "halt" in debug.process.argv
            attach = build_debug_plan(self.inputs(root, "attach"), PathPlanner(()))
            assert attach.gdb_argv is not None
            assert "load" not in attach.gdb_argv
            server = build_debug_plan(
                self.inputs(
                    root,
                    "debugserver",
                    serial="probe",
                    reset_halt="reset init",
                ),
                PathPlanner(()),
            )
            assert server.gdb_argv is None
            assert "set _ZEPHYR_BOARD_SERIAL probe" in server.process.argv
            assert server.process.argv.index(
                "set _ZEPHYR_BOARD_SERIAL probe"
            ) < server.process.argv.index("-f")
            assert "reset init" in server.process.argv
            for plan in (debug, attach, server):
                assert plan.process.completion_policy == CompletionPolicy.LIVE_SERVER
                assert plan.auxiliary_forwards == (
                    ForwardSpec("tcl", 6333, 6333),
                    ForwardSpec("telnet", 4444, 4444),
                )

    @pytest.mark.parametrize(
        "no_init",
        (False, True),
        ids=("explicit-init", "automatic-init"),
    )
    def test_openocd_readiness_setup_precedes_configs_and_requires_both_sentinels(
        self, tmp_path: Path, no_init: bool
    ):
        inputs = self.inputs(
            tmp_path,
            no_init=no_init,
            tcl_port="disabled",
            telnet_port="disabled",
        )
        # Board configs can call init; install the readiness hook before sourcing them.
        plan = build_debug_plan(inputs, PathPlanner(()))
        argv = plan.process.argv
        first_config = argv.index("-f")
        commands = [
            (index, argv[index + 1]) for index, argument in enumerate(argv[:-1]) if argument == "-c"
        ]
        pre_config = {command for index, command in commands if index < first_config}
        assert {
            "lappend post_init_commands {echo ZRO_OPENOCD_INIT_test}",
            f"bindto {REMOTE_ADDRESS_PLACEHOLDER}",
            "tcl_port disabled",
            "telnet_port disabled",
            "gdb_port 3333",
        } <= pre_config
        startup_commands = [command for index, command in commands if index > first_config]
        assert startup_commands[-1] == "echo ZRO_STARTUP_COMPLETE_test"
        assert ("init" in startup_commands) is (not no_init)
        if not no_init:
            assert startup_commands.index("init") < startup_commands.index(
                "echo ZRO_STARTUP_COMPLETE_test"
            )
        assert plan.process.required_output_sentinels == (
            "ZRO_OPENOCD_INIT_test",
            "ZRO_STARTUP_COMPLETE_test",
        )

    def test_local_and_remote_paths_with_spaces_remain_argv_elements(self, tmp_path: Path):
        local = tmp_path / "debug support"
        local.mkdir()
        config = local / "board config.cfg"
        config.write_text("# config\n")
        elf = tmp_path / "build output" / "zephyr image.elf"
        elf.parent.mkdir()
        elf.write_bytes(b"elf")
        plan = build_debug_plan(
            self.inputs(
                local,
                executable="/remote tools/openocd",
                gdb="/local tools/gdb",
                elf_file=str(elf),
                config_files=(str(config),),
            ),
            PathPlanner((PathMapping(local, PurePosixPath("/remote support")),)),
        )
        assert plan.process.argv[0] == "/remote tools/openocd"
        assert plan.process.argv[plan.process.argv.index("-s") + 1] == "/remote support"
        assert plan.process.argv[plan.process.argv.index("-f") + 1] == (
            "/remote support/board config.cfg"
        )
        assert plan.gdb_argv is not None
        assert plan.gdb_argv[0] == "/local tools/gdb"
        assert str(elf) in plan.gdb_argv

    @pytest.mark.parametrize("command", ("debug", "attach", "rtt", "debugserver"))
    def test_distinct_gdb_ports_preserve_target_selection(self, tmp_path: Path, command: str):
        plan = build_debug_plan(
            self.inputs(
                tmp_path,
                command=command,
                tcl_port="disabled",
                telnet_port="disabled",
                gdb_port="3344",
                gdb_client_port="3345",
                rtt_address=0x20000000,
            ),
            PathPlanner(()),
        )
        assert "gdb_port 3344" in plan.process.argv
        assert "tcl_port disabled" in plan.process.argv
        assert "telnet_port disabled" in plan.process.argv
        if command == "debugserver":
            assert plan.forwards == (ForwardSpec("gdb", 3344, 3344),)
            assert plan.gdb_argv is None
            assert not plan.reserved_listeners
        else:
            assert plan.forwards == (ForwardSpec("gdb", 3345, 3345),)
            assert plan.gdb_argv is not None
            assert "target extended-remote 127.0.0.1:3345" in plan.gdb_argv
            assert ListenerSpec("gdb_base", 3344) in plan.reserved_listeners
            if command == "rtt":
                assert plan.rtt_forward is not None
                assert plan.rtt_forward.listener in plan.reserved_listeners

    @pytest.mark.parametrize("client_port", ("disabled", "invalid", 4444))
    def test_debugserver_ignores_unused_gdb_client_port(self, tmp_path: Path, client_port):
        plan = build_debug_plan(
            self.inputs(tmp_path, command="debugserver", gdb_client_port=client_port),
            PathPlanner(()),
        )
        assert next(forward for forward in plan.forwards if forward.name == "gdb") == ForwardSpec(
            "gdb", 3333, 3333
        )
        assert plan.gdb_argv is None

    def test_disabled_gdb_server_fails_during_planning(self, tmp_path: Path):
        with pytest.raises(DebugPlanError):
            build_debug_plan(self.inputs(tmp_path, gdb_port="disabled"), PathPlanner(()))

    @pytest.mark.parametrize(
        "changes",
        (
            {"gdb_client_port": 4444},
            {"gdb_port": 4444},
        ),
        ids=("local", "remote"),
    )
    def test_ordinary_service_port_collisions_fail_during_planning(
        self, tmp_path: Path, changes: dict[str, int]
    ):
        with pytest.raises(DebugPlanError):
            build_debug_plan(self.inputs(tmp_path, **changes), PathPlanner(()))

    @pytest.mark.parametrize("rtt_port", (3333, 3334))
    def test_rtt_cannot_conflict_with_base_or_selected_gdb_listener(self, tmp_path, rtt_port):
        with pytest.raises(DebugPlanError):
            build_debug_plan(
                self.inputs(
                    tmp_path,
                    command="rtt",
                    gdb_client_port=3334,
                    rtt_address=0x20000000,
                    rtt_port=rtt_port,
                ),
                PathPlanner(()),
            )

    def test_version_parsing_and_thread_info_decision(self):
        old = parse_openocd_version("Open On-Chip Debugger 0.11.0")
        development = parse_openocd_version("Open On-Chip Debugger 0.11.0+dev")
        current = parse_openocd_version("Open On-Chip Debugger 0.12.0-01050")
        assert not thread_info_enabled(True, old)
        assert not thread_info_enabled(True, development)
        assert thread_info_enabled(True, current)
        assert not thread_info_enabled(False, None)
        with pytest.raises(DebugPlanError):
            parse_openocd_version("unknown")
        with pytest.raises(DebugPlanError):
            thread_info_enabled(True, None)

    def test_rtos_command_is_conditional_and_after_pre_init(self):
        with tempfile.TemporaryDirectory() as directory:
            version = parse_openocd_version("Open On-Chip Debugger 0.12.0")
            plan = build_debug_plan(
                self.inputs(
                    Path(directory),
                    pre_init=("adapter speed 1000",),
                    thread_info_requested=True,
                    openocd_version=version,
                ),
                PathPlanner(()),
            )
            argv = plan.process.argv
            assert argv.index("adapter speed 1000") < argv.index(
                "$_TARGETNAME configure -rtos Zephyr"
            )
            assert plan.rtos_awareness

    def test_standalone_rtt_uses_batch_gdb_and_deferred_service(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = build_debug_plan(
                self.inputs(
                    Path(directory),
                    "rtt",
                    rtt_address=0x20001000,
                    rtt_port=5566,
                    gdb_init=("set pagination off",),
                ),
                PathPlanner(()),
            )
            assert plan.gdb_argv is not None
            assert plan.process.completion_policy == CompletionPolicy.LIVE_SERVER
            forwards = {item.name: item for item in plan.forwards}
            assert forwards == {
                "gdb": ForwardSpec("gdb", 3333, 3333),
                "tcl": ForwardSpec("tcl", 6333, 6333),
                "telnet": ForwardSpec("telnet", 4444, 4444),
            }
            assert plan.rtt_forward == ForwardSpec("rtt", 5566, 5566)
            assert plan.reserved_listeners == (ListenerSpec("rtt", 5566),)
            assert plan.auxiliary_forwards == (
                ForwardSpec("tcl", 6333, 6333),
                ForwardSpec("telnet", 4444, 4444),
            )
            assert plan.rtt_forward not in plan.forwards
            assert plan.rtt_setup == "batch_gdb"
            assert plan.launches_rtt_client
            assert "--batch" in plan.gdb_argv
            assert plan.gdb_argv.index("set pagination off") < plan.gdb_argv.index(
                'monitor rtt setup 0x20001000 0x10 "SEGGER RTT"'
            )
            assert "monitor rtt server start 5566 0" in plan.gdb_argv
            assert "rtt server start 5566 0" not in plan.process.argv

    def test_rtt_server_is_ready_with_openocd_and_never_launches_client(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in ("debug", "debugserver"):
                plan = build_debug_plan(
                    self.inputs(
                        Path(directory),
                        command,
                        rtt_address=0x20002000,
                        rtt_port="5577",
                        rtt_server=True,
                    ),
                    PathPlanner(()),
                )
                forwards = {item.name: item for item in plan.forwards}
                assert forwards["rtt"] == ForwardSpec("rtt", 5577, 5577)
                assert plan.auxiliary_forwards == (
                    ForwardSpec("tcl", 6333, 6333),
                    ForwardSpec("telnet", 4444, 4444),
                )
                assert "rtt server start 5577 0" in plan.process.argv
                assert plan.rtt_setup == "openocd_startup"
                assert not plan.launches_rtt_client

    def test_rtt_requires_control_block_and_enabled_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with pytest.raises(DebugPlanError):
                build_debug_plan(self.inputs(root, "rtt"), PathPlanner(()))
            with pytest.raises(DebugPlanError):
                build_debug_plan(
                    self.inputs(root, "rtt", rtt_address=0x2000, rtt_port="disabled"),
                    PathPlanner(()),
                )
