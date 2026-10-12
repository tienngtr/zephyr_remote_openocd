# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ipaddress
import tempfile
from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.controller.model import Service
from zephyr_remote_openocd.controller.paths import (
    REMOTE_ADDRESS_PLACEHOLDER,
    PathPlanner,
    PathPlanningError,
)
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy
from zephyr_remote_openocd.protocol.process import ProcessSpec
from zephyr_remote_openocd.remote import flash as flash_module
from zephyr_remote_openocd.remote.debug import (
    DebugInputs,
    DebugPlanError,
    build_debug_plan,
    parse_openocd_version,
    thread_info_enabled,
)
from zephyr_remote_openocd.remote.flash import (
    FlashInputs,
    build_flash_plan,
)
from zephyr_remote_openocd.remote.services import (
    LOOPBACK_RANGE,
    allocate_loopback,
    random_loopback_address,
)

from tests.elf_fixtures import ELF_ENTRY_POINT, elf_memory_witness_bytes

TEST_PROCESS = ProcessSpec(("test-process",))


class TestAllocation:
    def test_range_and_exhaustion(self):
        assert ipaddress.IPv4Address(random_loopback_address()) in LOOPBACK_RANGE
        calls = []

        def collision(address):
            calls.append(address)
            raise OSError("occupied")

        with pytest.raises(RuntimeError):
            allocate_loopback(collision, attempts=3)
        assert len(calls) == 3


class TestFlashPlanning:
    @pytest.mark.parametrize(
        ("image_type", "load_command", "verify_command", "flash_address", "required"),
        (
            ("bin", None, None, None, ("load command", "flash address")),
            ("hex", None, None, None, ("load", "verify")),
        ),
    )
    def test_missing_required_flash_metadata_fails_during_planning(
        self,
        image_type: str,
        load_command: str | None,
        verify_command: str | None,
        flash_address: str | None,
        required: tuple[str, ...],
    ):
        planner = PathPlanner(())
        inputs = FlashInputs(
            executable="openocd",
            image_type=image_type,
            file=None,
            elf_file=None,
            hex_file=None,
            bin_file=None,
            search_paths=(),
            config_files=(),
            load_command=load_command,
            verify_command=verify_command,
            flash_address=flash_address,
        )

        with pytest.raises(flash_module.FlashPlanError) as error:
            build_flash_plan(inputs, planner)

        message = str(error.value).lower()
        assert all(item in message for item in required)
        assert not planner.staged_files
        assert not planner.remote_checks

    @pytest.mark.parametrize("kind", ("hex", "bin"))
    def test_unsupported_erase_fails_before_planning_paths(self, tmp_path: Path, kind: str):
        image = tmp_path / f"firmware.{kind}"
        image.write_bytes(b":00000001FF\n")
        planner = PathPlanner(())
        inputs = FlashInputs(
            executable="openocd",
            image_type=kind,
            file=str(image),
            elf_file=None,
            hex_file=str(image),
            bin_file=None,
            search_paths=(),
            config_files=(),
            load_command="program",
            verify_command="verify_image",
            flash_address="0x8000000",
            erase=True,
        )

        with pytest.raises(flash_module.FlashPlanError) as error:
            build_flash_plan(inputs, planner)

        message = str(error.value).lower()
        assert "erase requested" in message
        assert "no erase command" in message
        assert not planner.staged_files
        assert not planner.remote_checks

    def test_plan_directory_rejects_ancestor_symlink_cycle(self, tmp_path: Path):
        root = tmp_path / "search"
        ancestor = root / "a"
        ancestor.mkdir(parents=True)
        (ancestor / "back").symlink_to(root, target_is_directory=True)

        with pytest.raises(PathPlanningError):
            PathPlanner(()).plan_directory(root, "search_0")

    def test_hex_plan_preserves_ports_and_rewrites_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            support = root / "scripts" / "board"
            support.mkdir(parents=True)
            config = support / "openocd.cfg"
            config.write_text("source [find common.cfg]\n")
            (support / "common.cfg").write_text("# common\n")
            image = root / "zephyr.hex"
            image.write_text(":00000001FF\n")
            planner = PathPlanner(())
            plan = build_flash_plan(
                FlashInputs(
                    executable="/opt/openocd",
                    image_type="hex",
                    file=None,
                    elf_file=None,
                    hex_file=str(image),
                    bin_file=None,
                    search_paths=(str(root / "scripts"),),
                    config_files=(str(config),),
                    load_command="flash write_image erase",
                    verify_command="verify_image",
                    pre_init=("gdb_port 7777", "tcl_port 8888", "telnet_port 9999"),
                    verify=True,
                ),
                planner,
                (("PROBE", "value"),),
            )
            argv = plan.process.argv
            assert plan.process.completion_policy == CompletionPolicy.PROCESS_EXIT
            assert f"bindto {REMOTE_ADDRESS_PLACEHOLDER}" in argv
            assert "gdb_port 7777" in argv
            assert "gdb_port disabled" not in argv
            assert plan.process.environment == (("PROBE", "value"),)
            assert "{workspace}/staged/trees/search_0/board/openocd.cfg" in argv
            assert len([item for item in plan.staged_files if item.source == config]) == 1
            assert plan.process.argv[-4:] == ("-c", "reset run", "-c", "shutdown")

    def test_hex_explicit_erase_removes_implicit_load_erase(self, tmp_path: Path):
        image = tmp_path / "firmware.hex"
        image.write_bytes(b":00000001FF\n")

        plan = build_flash_plan(
            FlashInputs(
                executable="openocd",
                image_type="hex",
                file=None,
                elf_file=None,
                hex_file=str(image),
                bin_file=None,
                search_paths=(),
                config_files=(),
                load_command="flash write_image erase",
                verify_command="verify_image",
                erase=True,
                erase_commands=("target_erase",),
            ),
            PathPlanner(()),
        )
        commands = [
            plan.process.argv[index + 1]
            for index, argument in enumerate(plan.process.argv[:-1])
            if argument == "-c"
        ]

        erase_index = commands.index("target_erase")
        load_index = next(
            index
            for index, command in enumerate(commands)
            if command.startswith("flash write_image ")
        )
        assert erase_index < load_index
        load_command = commands[load_index]
        assert not load_command.startswith("flash write_image erase ")

    @pytest.mark.parametrize("kind", ("hex", "bin", "elf"))
    @pytest.mark.parametrize("erase_commands", ((), ("target_erase",)))
    def test_verify_only_plan_omits_load_and_erase_and_keeps_verify(
        self, tmp_path: Path, kind: str, erase_commands: tuple[str, ...]
    ):
        image = tmp_path / f"firmware.{kind}"
        image.write_bytes(
            elf_memory_witness_bytes(entry_point=ELF_ENTRY_POINT)
            if kind == "elf"
            else b":00000001FF\n"
        )

        plan = build_flash_plan(
            FlashInputs(
                executable="openocd",
                image_type=kind,
                file=str(image),
                elf_file=None,
                hex_file=str(image),
                bin_file=None,
                search_paths=(),
                config_files=(),
                load_command="flash write_image erase",
                verify_command="verify_image",
                flash_address="0x8000000",
                verify_only=True,
                erase=True,
                erase_commands=erase_commands,
            ),
            PathPlanner(()),
        )

        commands = {
            plan.process.argv[index + 1]
            for index, argument in enumerate(plan.process.argv[:-1])
            if argument == "-c"
        }
        assert any(command.startswith("verify_image ") for command in commands)
        assert not any(command.startswith("flash write_image ") for command in commands)
        assert not any(command.startswith("load_image ") for command in commands)
        assert "target_erase" not in commands

    @pytest.mark.parametrize("erase_commands", ((), ("target_erase",)))
    def test_elf_plan_ignores_erase_and_resumes_before_shutdown(
        self, tmp_path: Path, erase_commands: tuple[str, ...]
    ):
        image = tmp_path / "image.elf"
        image.write_bytes(elf_memory_witness_bytes(entry_point=ELF_ENTRY_POINT))
        plan = build_flash_plan(
            FlashInputs(
                executable="openocd",
                image_type="elf",
                file=None,
                elf_file=str(image),
                hex_file=None,
                bin_file=None,
                search_paths=(),
                config_files=(),
                erase=True,
                erase_commands=erase_commands,
            ),
            PathPlanner(()),
        )
        commands = tuple(
            plan.process.argv[index + 1]
            for index, argument in enumerate(plan.process.argv[:-1])
            if argument == "-c"
        )
        resume_commands = [command for command in commands if command.startswith("resume ")]
        assert len(resume_commands) == 1
        resume_command = resume_commands[0]
        assert int(resume_command.removeprefix("resume "), 0) == ELF_ENTRY_POINT
        assert commands.index(resume_command) < commands.index("shutdown")
        assert any(command.startswith("load_image ") for command in commands)
        assert "target_erase" not in commands

    def test_longest_mapping_and_remote_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specific = root / "specific"
            specific.mkdir()
            image = specific / "image.hex"
            image.write_text("image")
            planner = PathPlanner(
                (
                    PathMapping(root, PurePosixPath("/general")),
                    PathMapping(specific, PurePosixPath("/specific")),
                )
            )
            planned = planner.plan_file(image, "firmware")
            assert planned.remote == "/specific/image.hex"
            assert planner.remote_checks[0].path == "/specific/image.hex"

    def test_paths_with_spaces_remain_single_argv_and_quoted_tcl_words(self, tmp_path: Path):
        local = tmp_path / "local images"
        local.mkdir()
        image = local / "firmware image.hex"
        image.write_text(":00000001FF\n")
        config = local / "board config.cfg"
        config.write_text("# config\n")
        plan = build_flash_plan(
            FlashInputs(
                executable="/remote tools/openocd",
                image_type="hex",
                file=str(image),
                elf_file=None,
                hex_file=str(image),
                bin_file=None,
                search_paths=(str(local),),
                config_files=(str(config),),
                load_command="program",
                verify_command="verify_image",
            ),
            PathPlanner((PathMapping(local, PurePosixPath("/remote tree")),)),
        )
        argv = plan.process.argv
        assert argv[0] == "/remote tools/openocd"
        assert argv[argv.index("-s") + 1] == "/remote tree"
        assert argv[argv.index("-f") + 1] == "/remote tree/board config.cfg"
        assert 'program "/remote tree/firmware image.hex"' in argv
        assert plan.process.required_paths[-1].path == "/remote tree/firmware image.hex"

    def test_escaping_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            external = Path(outside) / "external.cfg"
            external.write_text("external")
            (root / "escape.cfg").symlink_to(external)
            with pytest.raises(PathPlanningError, match="escapes"):
                PathPlanner(()).plan_directory(root, "search_0")

    def test_bin_plan_preserves_address_erase_and_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.bin"
            image.write_bytes(b"binary")
            plan = build_flash_plan(
                FlashInputs(
                    executable="/openocd",
                    image_type="bin",
                    file=None,
                    elf_file=None,
                    hex_file=None,
                    bin_file=str(image),
                    search_paths=(),
                    config_files=(),
                    load_command="program",
                    verify_command="verify",
                    flash_address="0x8000000",
                    erase=True,
                    erase_commands=("mass_erase",),
                    verify=True,
                ),
                PathPlanner(()),
            )
            joined = "\n".join(plan.process.argv)
            assert "mass_erase" in joined
            assert "program " in joined
            assert "verify " in joined
            assert "0x8000000" in joined

    def test_serial_is_set_before_board_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "openocd.cfg"
            config.write_text("adapter driver ftdi\n")
            image = Path(directory) / "image.hex"
            image.write_text(":00000001FF\n")
            plan = build_flash_plan(
                FlashInputs(
                    executable="openocd",
                    image_type="hex",
                    file=str(image),
                    elf_file=None,
                    hex_file=str(image),
                    bin_file=None,
                    search_paths=(),
                    config_files=(str(config),),
                    load_command="program",
                    verify_command="verify_image",
                    serial="ES-FT4232H-02",
                ),
                PathPlanner(()),
            )
            argv = plan.process.argv
            assert argv.index("set _ZEPHYR_BOARD_SERIAL ES-FT4232H-02") < argv.index("-f")


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
                ),
                PathPlanner(()),
            )
            assert debug.gdb_argv is not None
            services = {item.name: item for item in debug.services}
            assert services == {
                "gdb": Service("gdb", 3333, 3333),
                "tcl": Service("tcl", 6333, 6333),
                "telnet": Service("telnet", 4444, 4444),
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
                assert plan.auxiliary_services == (
                    Service("tcl", 6333, 6333),
                    Service("telnet", 4444, 4444),
                )

    @pytest.mark.parametrize(
        ("no_init", "config_triggers_init"),
        ((False, False), (True, False), (True, True)),
        ids=("explicit-init", "automatic-init", "config-triggered-init"),
    )
    def test_openocd_readiness_setup_precedes_configs_and_requires_both_sentinels(
        self, tmp_path: Path, no_init: bool, config_triggers_init: bool
    ):
        inputs = self.inputs(
            tmp_path,
            no_init=no_init,
            tcl_port="disabled",
            telnet_port="disabled",
        )
        # Some board configs call `init` while being sourced; the readiness
        # hook and listener settings must precede those files too.
        config_file = tmp_path / "openocd.cfg"
        config_file.write_text("init\n" if config_triggers_init else "# config\n")
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
            assert plan.services == (Service("gdb", 3344, 3344),)
            assert plan.gdb_argv is None
            assert not plan.reserved_services
        else:
            assert plan.services == (Service("gdb", 3345, 3345),)
            assert plan.gdb_argv is not None
            assert "target extended-remote 127.0.0.1:3345" in plan.gdb_argv
            assert Service("gdb_base", 3344, 3344) in plan.reserved_services
            if command == "rtt":
                assert plan.rtt_service in plan.reserved_services

    @pytest.mark.parametrize("client_port", ("disabled", "invalid", 4444))
    def test_debugserver_ignores_unused_gdb_client_port(self, tmp_path: Path, client_port):
        plan = build_debug_plan(
            self.inputs(tmp_path, command="debugserver", gdb_client_port=client_port),
            PathPlanner(()),
        )
        assert next(service for service in plan.services if service.name == "gdb") == Service(
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
            services = {item.name: item for item in plan.services}
            assert services == {
                "gdb": Service("gdb", 3333, 3333),
                "tcl": Service("tcl", 6333, 6333),
                "telnet": Service("telnet", 4444, 4444),
            }
            assert plan.rtt_service == Service("rtt", 5566, 5566)
            assert plan.reserved_services == (Service("rtt", 5566, 5566),)
            assert plan.auxiliary_services == (
                Service("tcl", 6333, 6333),
                Service("telnet", 4444, 4444),
            )
            assert plan.rtt_service not in plan.services
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
                services = {item.name: item for item in plan.services}
                assert services["rtt"] == Service("rtt", 5577, 5577)
                assert plan.auxiliary_services == (
                    Service("tcl", 6333, 6333),
                    Service("telnet", 4444, 4444),
                )
                assert "rtt server start 5577 0" in plan.process.argv
                assert plan.rtt_setup == "openocd_startup"
                assert not plan.launches_rtt_client

    def test_rtt_requires_control_block_and_enabled_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with pytest.raises(DebugPlanError, match="RTT control block not found"):
                build_debug_plan(self.inputs(root, "rtt"), PathPlanner(()))
            with pytest.raises(DebugPlanError, match="rtt_port must be enabled"):
                build_debug_plan(
                    self.inputs(root, "rtt", rtt_address=0x2000, rtt_port="disabled"),
                    PathPlanner(()),
                )
            with pytest.raises(DebugPlanError, match="rtt_port conflicts"):
                build_debug_plan(
                    self.inputs(root, "rtt", rtt_address=0x2000, rtt_port=3333),
                    PathPlanner(()),
                )
