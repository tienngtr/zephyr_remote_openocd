# Hardware inventories

A hardware inventory connects external tests to a contributor's Zephyr
workspace, remote OpenOCD host, and physical targets.

> [!IMPORTANT]
> Populated inventories contain lab-specific data. Never put a populated
> inventory, or the hosts, probe identifiers, device paths, and serial
> captures it leads to, in commits, issues, or pull requests; see the
> [sensitive-data rules](../../CONTRIBUTING.md#sensitive-data). Use
> placeholders in committed examples.

## Quick start

1. Copy the flash-only starter into `.scratch/config/`. The repository excludes
   `.scratch/` from version control, so the populated inventory stays out of
   commits:

   ```sh
   mkdir -p .scratch/config
   cp tests/fixtures/hardware.example.yaml .scratch/config/hardware.yaml
   ```

2. Replace every placeholder with local paths, remote-host values, and the
   remote serial device path. The starter uses the STM32F746G Discovery board
   only as an example; replace the target name and `board` with the board
   connected to the remote host. The inventory must contain at least one host
   and one syntactically complete target record, even for SSH-only tests.
3. Validate the inventory. The validator reports schema, cross-reference, and
   local-path errors without contacting SSH, OpenOCD, serial devices, or
   hardware:

   ```sh
   .venv/bin/python scripts/contributor/validate_hardware_inventory.py \
     .scratch/config/hardware.yaml --check-local
   ```

   `--check-local` checks that local Zephyr, west, GDB, application, and host
   path-mapping paths exist. Omit it for an SSH-only inventory whose build fields remain
   placeholders.

4. Collect the external tests to see which `<target>:<profile>` nodes the
   inventory creates. Collection does not build, flash, debug, or access
   hardware:

   ```sh
   .venv/bin/python -m pytest --collect-only -q \
     tests/ssh_integration tests/hardware \
     --hardware-inventory .scratch/config/hardware.yaml
   ```

5. Run one node as described in [Running selected tests](#running-selected-tests).

To add debug, attach, debugserver, thread-info, RTT, or semihosting tests, copy
the operation from
[`hardware.complete.example.yaml`](../../tests/fixtures/hardware.complete.example.yaml)
together with the builds, `toolchains` entry, and profile settings it uses. Then
check its requirements under [Operations](#operations).

## Running selected tests

> [!WARNING]
> Hardware tests build, flash, halt, reset, and resume the configured targets.
> Collect first, as in the [Quick start](#quick-start), and run destructive
> profiles serially.

Start with one complete node ID copied from the `--collect-only` output. The
bracketed parameter is `<target>:<profile>`; the example uses target
`stm32f746g_disco` and profile `flash` from the starter inventory.
`--require-external-tests` makes the run fail if any test is skipped, so that a
missing prerequisite cannot pass unnoticed:

```sh
.venv/bin/python -m pytest --require-external-tests \
  'tests/hardware/test_real_flash.py::TestRealOpenOcdFlash::test_configured_target_flashes_and_emits_fresh_serial_output[stm32f746g_disco:flash]' \
  --hardware-inventory .scratch/config/hardware.yaml
```

Inspect cleanup output before reusing a target. For running whole layers, see
[Testing layers](testing.md#collect-and-run).

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

The schema is authoritative for structure. A file the editor accepts can still
fail the validator, which also checks cross-references and prerequisites that
JSON Schema cannot express.

## Inventory sections

| Section | Represents | Values run or exist on |
| --- | --- | --- |
| `build_environments` | Zephyr trees and west commands | Contributor machine |
| `toolchains` | Architecture-specific GDB commands | Contributor machine |
| `hosts` | SSH and OpenOCD configuration | SSH is local; OpenOCD is remote |
| `targets` | Boards connected to a named host | Physical lab |
| `targets.<target>.builds` | Zephyr applications and build arguments | Contributor machine |
| `targets.<target>.serial` | Device path and framing | Remote host |
| `targets.<target>.profiles` | Shared settings and enabled operations | Test orchestration |

Each key directly under these sections, such as `lab` under `hosts`, is an
identifier: other entries use it, for example a target's `host: lab`, and
target and profile names appear in test node IDs. Names must start with a
letter and contain only letters, digits, underscores, or hyphens.

SSH integration tests use the first declared host.

## Builds and profiles

### Targets and builds

Each target references one `host` and one `build_environment`. A target-level
`board` applies to all its builds; omit it only when every build supplies a
board override. Relative application paths resolve beneath `zephyr_base`, may
not contain `..`, and do not expand `~`. Absolute application paths may name
external trees.

### Build environment variables

Declare every SDK, toolchain, or CMake variable the build needs, such as
`ZEPHYR_TOOLCHAIN_VARIANT` and `ZEPHYR_SDK_INSTALL_DIR`, in the build
environment's `environment` mapping before running hardware tests. No other
ambient variables reach west. The mapping is optional.

| Variables | West builds | Operations and GDB clients | Build identity |
| --- | --- | --- | --- |
| Host baseline: `HOME`, `LANG`, `LC_ALL`, `PATH`, `PATHEXT`, `SYSTEMROOT`, `TEMP`, `TMP`, `TMPDIR`, when set | Yes | Yes | Yes |
| Build environment `environment` | Yes | Yes | Yes |
| Profile `environment`, also relayed to remote OpenOCD | No | Yes | No |
| SSH authentication inputs: `SSH_AUTH_SOCK`, `SSH_ASKPASS`, `SSH_ASKPASS_REQUIRE`, `DISPLAY`, `XAUTHORITY`, `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, `XDG_SESSION_TYPE`, `ICEAUTHORITY`, `SSHPASS`, when set | No | Yes | No |
| `ZEPHYR_BASE`, `EXTRA_ZEPHYR_MODULES` (the repository root), `ZEPHYR_REMOTE_OPENOCD_CONFIG` (the generated runner configuration); always set by the harness, overriding declared values | Yes | Yes | Not separately |
| `CCACHE_DIR`, `CCACHE_TEMPDIR`; default to the shared cache unless declared | Yes | No | Not separately |

> [!NOTE]
> Build identity includes the host baseline. Changing the toolchain selection
> starts a new build directory instead of reusing stale CMake state, and
> running pytest with a different ambient `PATH` or `TMPDIR` likewise selects
> new build and shared-cache directories and forces a cold build. Build
> identity also covers paths and build fields; see
> [Build cache](testing.md#build-cache).

If the configured SSH command needs a variable outside the authentication
inputs, declare it in the build environment's `environment` mapping. That makes
it part of the build identity, so changing its value forces a cold build.

### West and CMake arguments

`west_args` are appended after the application and `-d` build directory, before
the harness's `--pristine=never`. On cold builds, `cmake_args` are passed after
`--`; warm builds omit them and let west run Ninja directly. Both are arrays in
which each YAML item is one complete argument.

### Build output and caching

Hardware tests reuse incremental builds across pytest runs, under the ignored
`.scratch/hardware/` directory. The harness builds with `--pristine=never`, so a
cached build directory is never cleaned automatically. To force fresh builds,
delete the `.scratch/hardware/build-cache-*` directories; the shared Zephyr
and ccache data is kept. An explicit
`-DUSER_CACHE_DIR=...` in a build's `cmake_args` is used instead of the shared
Zephyr cache. For what the cache keys cover and how compatible inventories share
Zephyr and ccache directories, see [Build cache](testing.md#build-cache).

### Profiles

A profile selects one build. Every operation in that profile shares its build,
`probe_serial`, `runner_args`, and `environment`. `probe_serial` is appended to the
runner arguments as `--serial=<value>` unless `runner_args` already contain a
`--serial` argument. Profiles do not inherit
settings from each other. A profile `environment` key is valid only when the
selected host lists it in `forward_env`; the value is relayed literally to
remote OpenOCD.

### OpenOCD configuration files

Put files named by OpenOCD `--config` runner arguments in a directory that holds
only OpenOCD support files. Zephyr adds each config file's parent directory as
an OpenOCD search path, and the remote runner recursively stages every search
path not covered by the host's `path_mappings`. A config file beside
`.scratch/hardware/` or other unrelated content would therefore stage that
content too.

## Operations

Each key under a profile's `operations` enables hardware tests for that
profile, each collected as its own parametrized pytest node. Only profiles
selected for the run are built, and a build used by several selected profiles
is built once per pytest session.

| Operation | Required keys | Test effect |
| --- | --- | --- |
| `flash` | `precondition_build`, `serial` | Flashes both images and proves fresh serial output |
| `debug` | `precondition_build`, `breakpoint` (a symbol in an ELF with debug information) | Flashes the precondition, loads firmware, checks memory, and verifies a source-level stop |
| `attach` | `precondition_build` | Flashes it, attaches without loading, and checks memory |
| `debugserver` | Target `toolchain`, `precondition_build`, `serial` | Starts the GDB service and proves that local GDB controls the observed board |
| `thread_info` | `threads` (two or more patterns) | Flashes the build, attaches, and requires each pattern in GDB's thread list |
| `rtt` | Target `toolchain`, `port`, `input`, `response`, `timeout`, `program_survives_reset` | Exercises standalone `west rtt` and RTT servers under `west debug`, `west attach`, and `west debugserver` |
| `semihosting` | `commands`, `output`, `timeout` | Runs direct semihosting to natural completion |

The `serial` expectation of `flash` and `debugserver` holds an `endpoint`, which
names an entry under `targets.<target>.serial`, a `pattern`, and a `timeout`.

`flash`, `debugserver`, and `semihosting` operations also collect a
`--log-file` node that uses the same configuration. On Zephyr 4.4 builds these
nodes require the runner to reject `--log-file`. On Zephyr 4.5 builds the flash
and debugserver nodes stop the local `west` process while flooding OpenOCD's
log, and the semihosting node checks that console output stays on standard
output. See
[What hardware acceptance tests verify](testing.md#what-hardware-acceptance-tests-verify).

### Flash

- `precondition_build` names a different build that never prints text matching
  the serial `pattern`. The test assumes this and does not open a second serial
  session to check it.
- The serial `pattern` must be application-specific, so that the test can tell
  the selected image from stale firmware.
- Optional `output_patterns` are regular expressions that must each appear in
  the combined west and OpenOCD output. `debug` accepts them as well.
- `assert_bindto: true` requires the output to report the address remote
  OpenOCD bound to, inside the runner's `127.64.0.0/10` allocation range. The
  profile's `runner_args` must make OpenOCD report it, for example
  `--cmd-pre-init=bindto`, as in the complete example.

### Attach and debug

- `precondition_build` names a build whose ELF differs from the profile ELF in
  the bytes at one or more load addresses; otherwise the test fails before
  flashing anything. It need not be quiet.

The test uses one such address as a memory witness: after flashing the
precondition, attach requires that the witness still holds the precondition
bytes, and debug requires that `west debug` loaded the profile bytes before the
breakpoint.

### Debugserver

- The target references a `toolchain`, because the test launches GDB directly.
- `precondition_build` has the same distinct, quiet requirements as for flash.

The test:

1. flashes the precondition and starts debugserver;
2. arms the serial reader when the service is ready;
3. starts the profile build from an independent GDB client the way Zephyr's ELF
   flash does: `monitor reset init`, `load`, and `monitor resume` at the ELF
   entry;
4. requires the serial pattern.

A pass shows that the GDB session controls the board wired to the endpoint
named by the operation's `serial.endpoint`. With `probe_serial` set and several
probes on the host, it also shows that `debugserver --serial` selected the
requested probe; if debugserver controlled another probe, the observed board
would keep running the quiet precondition.

### Thread info

**Requirements**

- Enable `thread_info` only for a target that has Zephyr thread awareness;
  without it, GDB lists no application threads and the operation fails. Both
  components must support the target:
  - **OpenOCD build:** OpenOCD selects thread awareness by target type;
    upstream OpenOCD supports Cortex-M, Cortex-R4, and ARCv2 targets but not
    RISC-V.
  - **Runner:** on Zephyr 4.5 it requests thread awareness only for targets
    that upstream Zephyr's OpenOCD runner considers eligible (Cortex-M,
    AArch32 Cortex-R, and ARCv2); on Zephyr 4.4 it requests it for every
    target.
- GDB 12 or later as the GDB used by `west attach`, because the listing is
  logged with `set logging enabled`.
- A multithreaded application built with `CONFIG_DEBUG_THREAD_INFO=y` that names
  its threads, such as `samples/philosophers`, whose `Philosopher 0` and
  `Philosopher 1` threads the complete example uses.

**Matching rules**

The test flashes the profile build and attaches. Patterns are matched only
against GDB's `info threads` listing, which the test logs to a separate file so
that relayed OpenOCD output cannot split its lines.

- Each `threads` regular expression must fully match exactly one thread name,
  and no two patterns may match the same thread.
- A name counts whether GDB quotes it or shows it only in OpenOCD's
  `(Name: …)` description; a name shown only there that contains `, ` is
  truncated and does not match.
- Frames and other row text are ignored because they depend on where each
  thread stopped.
- A plain GDB remote session reports a single generic thread, so the patterns
  must name application threads that only OpenOCD's Zephyr thread awareness
  can discover.

### RTT

The RTT tests send `input` to the program over RTT and wait for `response` in
the target output. They run standalone `west rtt` and RTT servers under
`west debug`, `west attach`, and `west debugserver`.

- The target references a `toolchain`, because the tests launch GDB directly.
- `program_survives_reset` must be `true`, because standalone `west rtt` resets
  the target.
- `input` must be non-empty and safe to repeat: the variants that run under a
  GDB session resend it every second until the response arrives.
- `response` must be application-specific text that the program prints only in
  reply to `input`, never unprompted, so that a match proves transfer in both
  directions. The test assumes this and does not wait for a quiet period before
  sending the input.

### Semihosting

Semihosting needs an application that uses Zephyr's semihosting console
backend; the complete example uses Zephyr's console test with
`prj_semihost.conf`.

- `commands`: OpenOCD `--cmd-pre-init` commands that enable and configure
  semihosting.
- `gdb_commands`: optional GDB commands, run in order. They must let GDB and
  `west debug` exit naturally, with status 0, within `timeout`.
- `output`: a Python regular expression for application-specific text, searched
  for in the relayed output; escape any regular-expression metacharacters that
  must match literally.

## Defaults

Omitted fields take these values. The schema shows the same defaults to
editors; the inventory loader applies them at run time.

| Field | Default |
| --- | --- |
| `ssh_command` | `[ssh]` |
| `forward_env`, `path_mappings`, `toolchains`, `serial` | Empty |
| `west_args`, `cmake_args`, `runner_args`, build/profile `environment` | Empty |
| `data_bits`, `parity`, `stop_bits`, `flow_control` | `8`, `none`, `1`, `none` |
| `output_patterns`, `gdb_commands` | Empty |
| `assert_bindto` | `false` |
