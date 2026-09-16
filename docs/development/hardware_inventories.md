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
.venv/bin/python scripts/validate_hardware_inventory.py \
  .scratch/config/hardware.yaml --check-local
.venv/bin/python -m pytest --collect-only -q \
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

`west_args` are inserted before the application and CMake separator;
`cmake_args` are passed after `--`. Both are arrays in which each YAML item is
one complete argument.

A profile selects one build. Every operation in that profile shares its build,
`probe_serial`, `runner_args`, and `environment`. Profiles do not inherit. A
profile environment key is valid only when the selected host lists it in
`forward_env`. The value is relayed literally to remote OpenOCD.

## Defaults

| Field | Default |
| --- | --- |
| `ssh_command` | `[ssh]` |
| `forward_env`, `path_mappings`, `toolchains`, `serial` | Empty |
| `west_args`, `cmake_args`, `runner_args`, `environment` | Empty |
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
| `debug` | Breakpoint symbol in an ELF with debug information | Loads firmware and verifies a source-level stop |
| `attach` | Distinct precondition build | Flashes it, attaches without loading, and checks memory |
| `debugserver` | Target toolchain | Starts the GDB service and connects local GDB |
| `thread_info` | Expected thread-list pattern | Checks Zephyr-aware GDB thread output |
| `rtt` | Target toolchain, port, response, breakpoint | Exercises standalone and debug-integrated RTT |
| `semihosting` | OpenOCD commands, expected output, timeout | Runs direct semihosting to natural completion |

Flash uses an application-specific serial pattern to distinguish the selected
image from stale firmware. Its precondition build must be different from the
profile build. `output_patterns` match combined west/OpenOCD output;
`assert_bindto` additionally requires evidence that remote OpenOCD used its
allocated loopback address.

Attach has the same distinct-precondition requirement. It derives identifying
bytes from that ELF and verifies that attaching does not replace them.

RTT requires `program_survives_reset: true` because standalone `west rtt`
resets the target. Its input is sent through RTT and its response is matched in
the target output. RTT and debugserver require a target `toolchain` because the
tests launch GDB directly.

Thread-info works best with a multithreaded application such as
`samples/philosophers`. Semihosting requires an application configured for the
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
