# External hardware inventory

An external YAML inventory configures hardware, SSH, and serial acceptance
tests. It is not product configuration and must remain outside Git because it
contains host aliases, device paths, probe identities, and expected target
output.

Copy
[`tests/fixtures/hardware.example.yaml`](../../tests/fixtures/hardware.example.yaml)
to an ignored location, replace its placeholders, and validate collection:

```sh
python3 -m pytest --collect-only -q \
  --hardware-config /path/to/hardware.yaml
```

The same file is selected by `--hardware-config`, or by
`ZRO_HARDWARE_CONFIG` when the option is omitted. The command-line option wins.
Normal external tests skip when the inventory or a prerequisite is unavailable;
strict release validation treats missing required operations as a failure.

## Inventory structure

The inventory separates reusable local and remote resources from physical
targets:

- `build_environments` name local Zephyr checkouts and their `west` executable.
- `toolchains` name GDB executables used by direct debug clients.
- `hosts` define SSH, remote OpenOCD, forwarded environment allow-lists, and
  recursive path mappings. Host keys intentionally mirror product remote
  configuration.
- `targets` bind a host, build environment, optional toolchain, board, build
  recipes, serial endpoints, and operation profiles.

Profiles refer to a build recipe and contain an `operations` mapping. Operation
keys are the advertised capabilities used for pytest parametrization and release
evidence; there is no separate capability list. Profiles do not inherit from
one another.

Only selected profiles are prepared. Shared recipes are built once per test
session; deselected targets and recipes are not built or checked for local tool
availability. The complete inventory is still structurally and semantically
validated during collection.

The canonical example and
[`hardware.schema.json`](../../tests/fixtures/hardware.schema.json) define the
complete structural contract. Unknown keys, duplicate YAML keys, malformed
references, unsafe paths, and inconsistent operation prerequisites are errors.

## Flash and attach

A flash operation names a distinct precondition build and a structured serial
observation:

```yaml
profiles:
  flash:
    build: hello
    operations:
      flash:
        precondition_build: minimal
        quiescence_timeout: 2
        serial:
          endpoint: console
          pattern: hello
          timeout: 30
```

The test flashes the quiet precondition image, requires the selected image's
marker to remain absent for the quiescence interval, then flashes the selected
image and requires its marker. A relative recipe application resolves beneath
the selected `zephyr_base`; an absolute application path may name an external
tree.

An attach operation also names a distinct precondition build. The test flashes
that image, derives distinguishing bytes at ELF load addresses, and verifies
that `west attach` leaves those bytes intact.

The serial observer uses the referenced endpoint's baud, data bits, parity,
stop bits, and flow control. Its deadline includes the 180-second west operation
budget plus the operation's serial timeout. Use an application-specific pattern;
a generic banner cannot distinguish stale firmware.

## Debug, RTT, and semihosting

A debug operation supplies a source-level breakpoint symbol. Its ELF must
contain debug information and that C symbol. Debug and attach acceptance use
source-level GDB evidence rather than serial output.

RTT supplies a breakpoint for its debug variants and must declare
`program_survives_reset: true`, because standalone `west rtt` resets the target.
RTT and debugserver operations require a target toolchain because their tests
launch GDB directly. Fixtures that cannot support RTT or thread information
simply omit those operation keys.

Semihosting GDB commands must finish naturally within their configured timeout.
The test requires a successful west exit and matching relayed console output.
Generic interruption and SSH-loss cleanup remain covered by separate lifecycle
tests.

Never commit a populated inventory, generated build, serial capture,
credential, or remote workspace.
