# Hardware inventories

A hardware inventory connects external tests to a contributor's Zephyr
workspace, remote OpenOCD host, and physical targets. Populated inventories can
contain lab-specific paths and identities and must not be committed.

## Quick start

Create a local inventory from the flash-only starter:

```sh
mkdir -p .scratch/config
cp tests/fixtures/hardware.example.yaml .scratch/config/hardware.yaml
```

The repository excludes `.scratch/` from version control. Replace every
placeholder, validate the contract and local paths, and inspect test selection:

```sh
.venv/bin/python scripts/contributor/validate_hardware_inventory.py \
  .scratch/config/hardware.yaml --check-local
.venv/bin/python -m pytest --collect-only -q \
  tests/ssh_integration tests/hardware \
  --hardware-inventory .scratch/config/hardware.yaml
```

Validation does not contact SSH, OpenOCD, serial devices, or hardware.
Collection also performs no build, flash, debug, or hardware I/O.

Use
[`hardware.complete.example.yaml`](../../tests/fixtures/hardware.complete.example.yaml)
as the composition reference when adding debug, attach, debugserver,
thread-info, RTT, or semihosting.

## Editor assistance

The JSON Schema contains hover text, defaults, examples, completion choices,
and unknown-key validation. With the Red Hat YAML extension for VS Code, add
this association to workspace or user settings:

```json
{
  "yaml.schemas": {
    "./tests/fixtures/hardware.schema.json": [
      ".scratch/config/hardware*.yaml",
      "tests/fixtures/hardware*.yaml"
    ]
  }
}
```

The schema is authoritative for structure. The validator additionally enforces
cross-references and prerequisites that JSON Schema cannot express.

## Ownership and locality

| Section | Represents | Values run or exist on |
| --- | --- | --- |
| `build_environments` | Zephyr trees and west commands | Contributor machine |
| `toolchains` | Architecture-specific GDB commands | Contributor machine |
| `hosts` | SSH and OpenOCD configuration | SSH is local; OpenOCD is remote |
| `targets` | Boards connected to a named host | Physical lab |
| `builds` | Zephyr applications and build arguments | Contributor machine |
| `serial` | Device path and framing | Remote host |
| `profiles` | Shared settings and enabled operations | Test orchestration |

Names under these sections are references, not executable values. Names must
start with a letter and contain only letters, digits, underscores, or hyphens.

## Shared resources and builds

Each target references one `host` and one `build_environment`. A target-level
`board` applies to all its builds; omit it only when every build supplies a
board override. Relative application paths resolve beneath `zephyr_base` and
cannot escape that tree. Absolute application paths may name external trees.

Declare build-affecting variables such as `ZEPHYR_TOOLCHAIN_VARIANT` and
`ZEPHYR_SDK_INSTALL_DIR` in the build environment's `environment` mapping.
The harness passes a small host baseline (`HOME`, locale, `PATH`, platform
command lookup, and temporary-directory variables) plus those explicit values;
other ambient variables do not reach west. `ZEPHYR_BASE` is set to the selected
`zephyr_base`. Hardware operations and independent GDB clients use the same
restricted baseline and declared build environment, with profile environment
values and the harness-generated runner configuration applied for the operation.
Operations additionally retain SSH agent, askpass/desktop-session, and sshpass
inputs needed by the configured SSH command. These authentication inputs are
excluded from build preparation and cache identity unless explicitly declared.
Ambient product configuration, remote selection, and recording controls are
excluded. Both sets participate in persistent
build and shared-cache identity, so changing toolchain selection cannot reuse
stale CMake state.

The mapping is optional. Declare any SDK, toolchain, or CMake variables needed
by the build in this mapping before running hardware tests.

`west_args` are inserted before the application and CMake separator;
`cmake_args` are passed after `--`. Both are arrays in which each YAML item is
one complete argument.

Keep files named by OpenOCD `--config` runner arguments in a dedicated support
directory. Zephyr adds each config file's parent as an OpenOCD search path, and
the remote runner recursively stages unmapped search paths. Do not place those
files directly beside `.scratch/hardware/` build caches or other unrelated
content.

Hardware tests keep their build output under the repository's ignored
`.scratch/hardware/` directory. Build namespaces include the checkout and
build-relevant inventory fields, while compatible inventories share
environment-scoped Zephyr and ccache directories. The test harness uses
`--pristine=never`; an explicit `-DUSER_CACHE_DIR=...` in a build's
`cmake_args` is preserved. Remove the matching `build-cache-<build-input-hash>`
directory when a fresh build is required.

A profile selects one build. Every operation in that profile shares its build,
`probe_serial`, `runner_args`, and `environment`. Profiles do not inherit. A
profile environment key is valid only when the selected host lists it in
`forward_env`. The value is relayed literally to remote OpenOCD.

## Defaults

| Field | Default |
| --- | --- |
| `ssh_command` | `[ssh]` |
| `forward_env`, `path_mappings`, `toolchains`, `serial` | Empty |
| `west_args`, `cmake_args`, `runner_args`, build/profile `environment` | Empty |
| `data_bits`, `parity`, `stop_bits`, `flow_control` | `8`, `none`, `1`, `none` |
| `output_patterns`, `gdb_commands` | Empty |
| `assert_bindto` | `false` |
| RTT `input` | Empty string |

Schema defaults are editor hints; the inventory loader applies the matching
runtime defaults.

## Operations

Operation keys create separate parametrized pytest nodes. Only selected
profiles are prepared, and a shared build recipe is built once per test
session.

| Operation | Required configuration | Test effect |
| --- | --- | --- |
| `flash` | Distinct precondition build, serial expectation | Flashes both images and proves fresh serial output |
| `debug` | Distinct precondition build, breakpoint symbol in an ELF with debug information | Flashes the precondition, loads firmware, checks memory, and verifies a source-level stop |
| `attach` | Distinct precondition build | Flashes it, attaches without loading, and checks memory |
| `debugserver` | Target toolchain, distinct precondition build, serial expectation | Starts the GDB service and proves that local GDB controls the observed board |
| `thread_info` | Two or more application thread patterns | Flashes the build, attaches, and requires each pattern in GDB's thread list |
| `rtt` | Target toolchain, port, response, breakpoint | Exercises `west rtt` and debug-integrated RTT |
| `semihosting` | OpenOCD commands, expected output, timeout | Runs direct semihosting to natural completion |

Flash uses an application-specific serial pattern to distinguish the selected
image from stale firmware. Its precondition build must be different from the
profile build. `output_patterns` match combined west/OpenOCD output;
`assert_bindto` additionally requires evidence that remote OpenOCD used its
allocated loopback address. The precondition must be quiet with respect to the
selected image pattern; the harness treats that as a test invariant rather
than opening a second serial session to observe a timed quiet period.

Debugserver's `serial` expectation proves that the GDB session controls the
board wired to that endpoint. Its `precondition_build` has the same distinct,
quiet requirements as flash. The harness flashes the
precondition and starts debugserver. When the service is ready, it arms the
reader. An independent GDB client then starts the profile build through
debugserver the way Zephyr's ELF flash does: `monitor reset init`, `load`, and
`monitor resume` at the ELF entry. The pattern must then appear. With `probe_serial` set and
several probes on the host, this shows that `debugserver --serial` selected the
requested probe. If debugserver controlled another probe, the observed board
would keep running the quiet precondition.

Attach and debug have the same distinct-precondition requirement, but not the
quietness requirement. Each derives a memory witness: bytes at one load address
that differ between the precondition and profile ELFs. After flashing the
precondition, attach requires that the witness still holds the precondition
bytes, and debug requires that `west debug` loaded the profile bytes before
the breakpoint. A precondition with no such witness fails the test before it
flashes anything.

RTT requires `program_survives_reset: true` because the `west rtt` command
resets the target. Its input is sent through RTT and its response is matched in
the target output. RTT and debugserver require a target `toolchain` because the
tests launch GDB directly.

Thread-info flashes the profile build, attaches, and brackets GDB's
`info threads` output with test-owned markers. Each `threads` regular
expression must match exactly one row of that list, and no two patterns may
match the same row. A plain GDB remote session reports a single generic
thread, so the patterns must name application threads that only OpenOCD's
Zephyr thread awareness can discover. Use a multithreaded application built
with `CONFIG_DEBUG_THREAD_INFO=y` that names its threads, such as
`samples/philosophers`, whose `Philosopher 0` and `Philosopher 1` threads the
complete example uses. Semihosting requires an application configured for the
console backend; the complete example uses Zephyr's console test with
`prj_semihost.conf`. Semihosting GDB commands must allow the process to finish
naturally within its timeout.

## Running selected tests

External tests can build, flash, halt, reset, and resume targets. Always collect
first and run destructive profiles serially. Start with one complete node ID:

```sh
.venv/bin/python -m pytest --require-external-tests \
  'tests/hardware/test_real_flash.py::TestRealOpenOcdFlash::test_configured_target_flashes_and_emits_fresh_serial_output[stm32f746g_disco:flash]' \
  --hardware-inventory .scratch/config/hardware.yaml
```

Inspect cleanup output before reusing a target. Never commit a populated
inventory, generated build, serial capture, credential, remote host identity,
probe identifier, device path, or remote workspace.
