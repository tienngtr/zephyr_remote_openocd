# SPDX-License-Identifier: Apache-2.0

"""Pure construction of a remotely executable Zephyr OpenOCD flash plan."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model import RemoteProcess
from .openocd_plan import OpenOcdBasePlan, plan_openocd_base
from .paths import PathPlanner


class FlashPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class FlashInputs:
    executable: str | tuple[str, ...]
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


@dataclass(frozen=True)
class PlannedFlashImage:
    """Resolved image information used by the operation-specific planners."""

    kind: str
    source: Path
    remote: str
    quoted: str
    entry: str | None = None


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


def _image_kind(inputs: FlashInputs) -> str:
    image_type = inputs.image_type.lower() if inputs.image_type else None
    return image_type or "hex"


def _image_source(inputs: FlashInputs, kind: str) -> str | None:
    if inputs.file is not None:
        return inputs.file
    if kind == "elf":
        return inputs.elf_file
    if kind == "bin":
        return inputs.bin_file
    return inputs.hex_file


def _validate_flash_options(inputs: FlashInputs, kind: str) -> None:
    if kind == "bin" and (not inputs.load_command or inputs.flash_address is None):
        raise FlashPlanError("cannot flash BIN; load command and flash address are required")
    if kind not in ("elf", "bin") and (not inputs.load_command or not inputs.verify_command):
        raise FlashPlanError("cannot flash image; load and verify commands are required")
    if inputs.erase and not inputs.erase_commands:
        raise FlashPlanError("erase requested but the target supplies no erase command")


def _plan_image(inputs: FlashInputs, planner: PathPlanner) -> PlannedFlashImage:
    kind = _image_kind(inputs)
    image_source = _image_source(inputs, kind)
    if not image_source:
        raise FlashPlanError(f"cannot flash; no {kind} image specified")
    source_path = Path(image_source).resolve()
    remote_image = planner.plan_file(source_path, "firmware").remote
    entry = _elf_entry(source_path) if kind == "elf" else None
    return PlannedFlashImage(kind, source_path, remote_image, _tcl_quote(remote_image), entry)


def _common_flash_commands(inputs: FlashInputs) -> list[str]:
    commands = _commands(inputs.pre_init)
    if not inputs.no_init:
        commands.extend(("-c", "init"))
    if not inputs.no_targets:
        commands.extend(("-c", "targets"))
    return commands


def _elf_flash_commands(inputs: FlashInputs, image: PlannedFlashImage) -> tuple[str, ...]:
    commands: list[str] = []
    if not inputs.verify_only:
        commands.extend(_commands(inputs.pre_load))
        commands.extend(("-c", inputs.reset_halt, "-c", f"load_image {image.quoted}"))
    if inputs.verify or inputs.verify_only:
        commands.extend(("-c", f"verify_image {image.quoted}"))
        commands.extend(_commands(inputs.post_verify))
    commands.extend(("-c", f"resume {image.entry}", "-c", "shutdown"))
    return tuple(commands)


def _finish_standard_commands(commands: list[str], inputs: FlashInputs) -> tuple[str, ...]:
    commands.extend(_commands(inputs.post_verify))
    commands.extend(("-c", "reset run", "-c", "shutdown"))
    return tuple(commands)


def _bin_flash_commands(inputs: FlashInputs, image: PlannedFlashImage) -> tuple[str, ...]:
    commands = _commands(inputs.pre_load)
    if not inputs.verify_only:
        commands.extend(("-c", inputs.reset_halt))
        if inputs.erase:
            commands.extend(_commands(inputs.erase_commands))
        commands.extend(("-c", f"{inputs.load_command} {image.quoted} {inputs.flash_address}"))
    if (inputs.verify or inputs.verify_only) and inputs.verify_command:
        commands.extend(("-c", inputs.reset_halt))
        commands.extend(("-c", f"{inputs.verify_command} {image.quoted} {inputs.flash_address}"))
    return _finish_standard_commands(commands, inputs)


def _hex_flash_commands(inputs: FlashInputs, image: PlannedFlashImage) -> tuple[str, ...]:
    commands = _commands(inputs.pre_load)
    load_command = inputs.load_command or ""
    if not inputs.verify_only:
        commands.extend(("-c", inputs.reset_halt))
        if inputs.erase:
            commands.extend(_commands(inputs.erase_commands))
            if load_command.endswith(" erase"):
                load_command = load_command[:-6]
        commands.extend(("-c", f"{load_command} {image.quoted}"))
    if inputs.verify or inputs.verify_only:
        commands.extend(("-c", inputs.reset_halt, "-c", f"{inputs.verify_command} {image.quoted}"))
    return _finish_standard_commands(commands, inputs)


def _operation_commands(inputs: FlashInputs, image: PlannedFlashImage) -> tuple[str, ...]:
    if image.kind == "elf":
        return _elf_flash_commands(inputs, image)
    if image.kind == "bin":
        return _bin_flash_commands(inputs, image)
    return _hex_flash_commands(inputs, image)


def _flash_argv(
    inputs: FlashInputs, base: OpenOcdBasePlan, image: PlannedFlashImage
) -> tuple[str, ...]:
    return base.argv + tuple(_common_flash_commands(inputs)) + _operation_commands(inputs, image)


def build_flash_plan(
    inputs: FlashInputs,
    planner: PathPlanner,
    environment: tuple[tuple[str, str], ...] = (),
) -> FlashPlan:
    kind = _image_kind(inputs)
    _validate_flash_options(inputs, kind)
    base = plan_openocd_base(
        inputs.executable,
        inputs.serial,
        inputs.search_paths,
        inputs.config_files,
        planner,
    )
    image = _plan_image(inputs, planner)

    process = RemoteProcess(
        "openocd",
        _flash_argv(inputs, base, image),
        environment,
        tuple(planner.remote_checks),
        literal_prefix=base.literal_prefix,
    )
    return FlashPlan(process, tuple(planner.staged_files), image.remote)
