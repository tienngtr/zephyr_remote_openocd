# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import tempfile
from pathlib import Path, PurePosixPath

import pytest
from zephyr_remote_openocd.config import PathMapping
from zephyr_remote_openocd.controller.paths import (
    REMOTE_ADDRESS_PLACEHOLDER,
    PathPlanner,
    PathPlanningError,
)
from zephyr_remote_openocd.openocd import flash as flash_module
from zephyr_remote_openocd.openocd.flash import (
    FlashInputs,
    FlashMode,
    FlashOperation,
    build_flash_plan,
)
from zephyr_remote_openocd.protocol.outcome import CompletionPolicy

from tests.elf_fixtures import ELF_ENTRY_POINT, elf_memory_witness_bytes


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
    ) -> None:
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

    @pytest.mark.parametrize("kind", ("hex", "bin", "other"))
    def test_unsupported_erase_fails_before_planning_paths(self, tmp_path: Path, kind: str) -> None:
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
            operation=FlashOperation(erase=True),
        )

        with pytest.raises(flash_module.FlashPlanError) as error:
            build_flash_plan(inputs, planner)

        message = str(error.value).lower()
        assert "erase requested" in message
        assert "no erase command" in message
        assert not planner.staged_files
        assert not planner.remote_checks

    def test_verify_only_operation_cannot_erase(self) -> None:
        # Verify-only never programs, so an erase cannot be represented at all.
        with pytest.raises(flash_module.FlashPlanError):
            FlashOperation(FlashMode.VERIFY_ONLY, erase=True)

    @pytest.mark.parametrize("mode", (FlashMode.PROGRAM_AND_VERIFY, FlashMode.VERIFY_ONLY))
    def test_bin_verification_without_verify_command_fails_before_planning_paths(
        self, tmp_path: Path, mode: FlashMode
    ) -> None:
        image = tmp_path / "firmware.bin"
        image.write_bytes(b"\x00")
        planner = PathPlanner(())
        inputs = FlashInputs(
            executable="openocd",
            image_type="bin",
            file=str(image),
            elf_file=None,
            hex_file=None,
            bin_file=str(image),
            search_paths=(),
            config_files=(),
            load_command="program",
            flash_address="0x8000000",
            operation=FlashOperation(mode),
        )

        with pytest.raises(flash_module.FlashPlanError) as error:
            build_flash_plan(inputs, planner)

        assert "no verify command" in str(error.value).lower()
        assert not planner.staged_files
        assert not planner.remote_checks

    @pytest.mark.parametrize("spelling", ("ghost/../zephyr.bin", "zephyr.bin/", "zephyr.bin/."))
    def test_unreachable_image_spelling_is_not_flashed(self, tmp_path: Path, spelling: str) -> None:
        (tmp_path / "zephyr.bin").write_bytes(b"binary")
        planner = PathPlanner(())
        inputs = FlashInputs(
            executable="/openocd",
            image_type="bin",
            file=None,
            elf_file=None,
            hex_file=None,
            bin_file=f"{tmp_path}/{spelling}",
            search_paths=(),
            config_files=(),
            load_command="program",
            flash_address="0x8000000",
        )

        with pytest.raises(PathPlanningError):
            build_flash_plan(inputs, planner)
        assert not planner.staged_files

    def test_plan_directory_rejects_ancestor_symlink_cycle(self, tmp_path: Path) -> None:
        root = tmp_path / "search"
        ancestor = root / "a"
        ancestor.mkdir(parents=True)
        (ancestor / "back").symlink_to(root, target_is_directory=True)

        with pytest.raises(PathPlanningError):
            PathPlanner(()).plan_directory(root, "search_0")

    def test_hex_plan_preserves_ports_and_rewrites_paths(self) -> None:
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
                    operation=FlashOperation(FlashMode.PROGRAM_AND_VERIFY),
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
            staged_configs = [item for item in plan.staged_files if item.source == config]
            assert len(staged_configs) == 1
            assert f"{{workspace}}/staged/{staged_configs[0].destination}" in argv
            assert plan.process.argv[-4:] == ("-c", "reset run", "-c", "shutdown")

    def test_hex_explicit_erase_removes_implicit_load_erase(self, tmp_path: Path) -> None:
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
                operation=FlashOperation(erase=True),
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
    def test_verify_only_plan_omits_load_and_keeps_verify(self, tmp_path: Path, kind: str) -> None:
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
                operation=FlashOperation(FlashMode.VERIFY_ONLY),
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

    @pytest.mark.parametrize("erase_commands", ((), ("target_erase",)))
    def test_elf_erase_fails_before_planning_paths(
        self, tmp_path: Path, erase_commands: tuple[str, ...]
    ) -> None:
        image = tmp_path / "image.elf"
        image.write_bytes(elf_memory_witness_bytes(entry_point=ELF_ENTRY_POINT))
        planner = PathPlanner(())
        inputs = FlashInputs(
            executable="openocd",
            image_type="elf",
            file=None,
            elf_file=str(image),
            hex_file=None,
            bin_file=None,
            search_paths=(),
            config_files=(),
            operation=FlashOperation(erase=True),
            erase_commands=erase_commands,
        )

        # Rejected even when an erase command exists: ELF loading never erases.
        with pytest.raises(flash_module.FlashPlanError):
            build_flash_plan(inputs, planner)

        assert not planner.staged_files
        assert not planner.remote_checks

    def test_elf_plan_resumes_before_shutdown(self, tmp_path: Path) -> None:
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

    def test_longest_mapping_and_remote_check(self) -> None:
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

    def test_mapping_does_not_match_sibling_with_shared_name_prefix(self, tmp_path: Path) -> None:
        root = tmp_path.resolve()
        (root / "src").mkdir()
        sibling = root / "src2"
        sibling.mkdir()
        image = sibling / "firmware.hex"
        image.write_text(":00000001FF\n")
        planner = PathPlanner((PathMapping(root / "src", PurePosixPath("/remote/a")),))

        planned = planner.plan_file(image, "firmware")

        assert not planned.remote.startswith("/remote/a")
        assert not planner.remote_checks
        assert [entry.source for entry in planner.staged_files] == [image]

    def test_paths_with_spaces_remain_single_argv_and_quoted_tcl_words(
        self, tmp_path: Path
    ) -> None:
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

    def test_escaping_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            external = Path(outside) / "external.cfg"
            external.write_text("external")
            (root / "escape.cfg").symlink_to(external)
            with pytest.raises(PathPlanningError):
                PathPlanner(()).plan_directory(root, "search_0")

    def test_bin_plan_preserves_address_erase_and_verify(self) -> None:
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
                    operation=FlashOperation(FlashMode.PROGRAM_AND_VERIFY, erase=True),
                    erase_commands=("mass_erase",),
                ),
                PathPlanner(()),
            )
            joined = "\n".join(plan.process.argv)
            assert "mass_erase" in joined
            assert "program " in joined
            assert "verify " in joined
            assert "0x8000000" in joined

    def test_serial_is_set_before_board_configuration(self) -> None:
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
