# SPDX-License-Identifier: Apache-2.0

"""Pure construction of a remotely executable Zephyr OpenOCD flash plan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model import RemoteProcess
from .paths import ADDRESS_TOKEN, PathPlanner


class FlashPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class FlashInputs:
    executable: str
    image_type: str | None
    file: str | None
    elf_file: str | None
    hex_file: str | None
    bin_file: str | None
    search_paths: tuple[str, ...]
    config_files: tuple[str, ...]
    pre_init: tuple[str, ...] = ()
    reset_halt: str = "reset init"
    pre_load: tuple[str, ...] = ()
    erase_commands: tuple[str, ...] = ()
    load_command: str | None = None
    verify_command: str | None = None
    post_verify: tuple[str, ...] = ()
    verify: bool = False
    verify_only: bool = False
    erase: bool = False
    serial: str | None = None
    flash_address: str | None = None
    no_init: bool = False
    no_targets: bool = False


@dataclass(frozen=True)
class FlashPlan:
    process: RemoteProcess
    staged_files: tuple
    image: str


def _commands(commands: tuple[str, ...]) -> list[str]:
    return [item for command in commands for item in ("-c", command)]


def _tcl_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("$", "\\$").replace("[", "\\[").replace("]", "\\]")
    return f'"{escaped}"'


def _elf_entry(path: Path) -> str:
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError as error:
        raise FlashPlanError(
            "pyelftools (elftools) is required for ELF flashing; "
            "use the supported Zephyr Python environment"
        ) from error
    try:
        with path.open("rb") as stream:
            return f"0x{ELFFile(stream).header['e_entry']:016x}"
    except OSError as error:
        raise FlashPlanError(f"cannot read ELF image {path}: {error}") from error


def build_flash_plan(
    inputs: FlashInputs,
    planner: PathPlanner,
    environment: tuple[tuple[str, str], ...] = (),
) -> FlashPlan:
    image_type = inputs.image_type.lower() if inputs.image_type else None
    image_source = inputs.file
    if image_source is None:
        if image_type == "elf":
            image_source = inputs.elf_file
        elif image_type == "bin":
            image_source = inputs.bin_file
        else:
            image_source = inputs.hex_file
    if not image_source:
        raise FlashPlanError(f"cannot flash; no {image_type or 'hex'} image specified")
    source_path = Path(image_source).resolve()

    if image_type == "bin" and (not inputs.load_command or inputs.flash_address is None):
        raise FlashPlanError("cannot flash BIN; load command and flash address are required")
    if image_type not in ("elf", "bin") and (not inputs.load_command or not inputs.verify_command):
        raise FlashPlanError("cannot flash image; load and verify commands are required")
    if inputs.erase and not inputs.erase_commands:
        raise FlashPlanError("erase requested but the target supplies no erase command")

    indexed_search = list(enumerate(inputs.search_paths))
    planned_search = {}
    for index, path in sorted(indexed_search, key=lambda item: len(Path(item[1]).resolve().parts)):
        planned_search[index] = planner.plan_directory(Path(path), f"search-{index}").remote
    remote_search = [planned_search[index] for index, _ in indexed_search]
    remote_configs = [
        planner.plan_file(Path(path), f"config-{index}").remote
        for index, path in enumerate(inputs.config_files)
    ]
    remote_image = planner.plan_file(source_path, "firmware").remote

    argv = [inputs.executable]
    # Zephyr's OpenOCD runner sets the board serial before loading the board
    # configuration.  The configuration may consume _ZEPHYR_BOARD_SERIAL
    # while it is being evaluated (for example via ``adapter serial``).
    if inputs.serial:
        argv.extend(("-c", "set _ZEPHYR_BOARD_SERIAL " + inputs.serial))
    for path in remote_search:
        argv.extend(("-s", path))
    for path in remote_configs:
        argv.extend(("-f", path))
    argv.extend(("-c", f"bindto {ADDRESS_TOKEN}"))
    argv.extend(_commands(inputs.pre_init))
    if not inputs.no_init:
        argv.extend(("-c", "init"))
    if not inputs.no_targets:
        argv.extend(("-c", "targets"))

    quoted_image = _tcl_quote(remote_image)
    if image_type == "elf":
        entry = _elf_entry(source_path)
        if not inputs.verify_only:
            argv.extend(_commands(inputs.pre_load))
            argv.extend(("-c", inputs.reset_halt, "-c", f"load_image {quoted_image}"))
        if inputs.verify or inputs.verify_only:
            argv.extend(("-c", f"verify_image {quoted_image}"))
            argv.extend(_commands(inputs.post_verify))
        argv.extend(("-c", f"resume {entry}", "-c", "shutdown"))
    else:
        argv.extend(_commands(inputs.pre_load))
        load_command = inputs.load_command
        if not inputs.verify_only:
            argv.extend(("-c", inputs.reset_halt))
            if inputs.erase:
                argv.extend(_commands(inputs.erase_commands))
                if image_type != "bin" and load_command and load_command.endswith(" erase"):
                    load_command = load_command[:-6]
            suffix = f" {inputs.flash_address}" if image_type == "bin" else ""
            argv.extend(("-c", f"{load_command} {quoted_image}{suffix}"))
        if inputs.verify or inputs.verify_only:
            if image_type == "bin" and inputs.verify_command:
                argv.extend(
                    (
                        "-c",
                        inputs.reset_halt,
                        "-c",
                        f"{inputs.verify_command} {quoted_image} {inputs.flash_address}",
                    )
                )
            elif image_type != "bin":
                argv.extend(
                    ("-c", inputs.reset_halt, "-c", f"{inputs.verify_command} {quoted_image}")
                )
        argv.extend(_commands(inputs.post_verify))
        argv.extend(("-c", "reset run", "-c", "shutdown"))

    process = RemoteProcess("openocd", tuple(argv), environment, tuple(planner.remote_checks))
    return FlashPlan(process, tuple(planner.staged_files), remote_image)
