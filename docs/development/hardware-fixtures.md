# External hardware inventory

Hardware, SSH, and serial acceptance tests are intentionally data-driven. The
inventory file is not product configuration and must remain outside the Git
tree because it contains host aliases, device paths, probe identities, and
expected target output.

Start with [`tests/fixtures/hardware.example.toml`](../../tests/fixtures/hardware.example.toml),
copy it to an ignored location, and replace its neutral placeholders. Validate
and select it with:

```sh
python3 -m pytest --collect-only -q --hardware-config /path/to/hardware.toml
```

The same file is selected by `--hardware-config`, or by
`ZRO_HARDWARE_CONFIG` when the option is omitted. The command-line option wins.
Normal external tests skip when the inventory or a prerequisite is unavailable;
strict release validation treats missing required capabilities as a failure.

Each `hosts` entry describes the SSH argv, absolute remote OpenOCD executable,
forwarded environment allow-list, and recursive path mappings. Each `targets`
entry names its Zephyr tree, west/GDB tools, build recipes, serial endpoints,
and independent operation profiles. Profiles refer to a recipe and may declare
probe selection, environment values (only names in the host allow-list),
structured serial expectations, RTT/semihosting observations, and capabilities.

A relative recipe application resolves beneath `zephyr_base`; an absolute
application path is allowed for an external tree. Profiles do not inherit from
one another. Unsupported RTT or thread-info capability therefore skips only
that capability while retaining other operations for the target.

Only profiles selected for execution are prepared. Shared build recipes are
built once per test session; deselected targets and recipes are not built or
checked for local tool availability. The complete inventory is still validated
for structural and reference errors during collection.

Use the fixture's declared serial framing (baud, data bits, parity, stop bits,
and flow control) rather than embedding device knowledge in test code. Never
commit a populated inventory, generated build, serial capture, credential, or
remote workspace.

A profile advertising `debug` must declare a source-level breakpoint symbol:

```toml
[targets.profiles.debug.debug]
breakpoint = "main"
```

The profile may select any build recipe; it is not restricted to
`samples/hello_world` or `samples/basic/minimal`. Its ELF must contain debug
information and the configured C symbol. Real debug acceptance uses GDB to load
the ELF, continue without resetting, stop at that symbol, and inspect the
program counter and current instruction. GDB and OpenOCD may implement the
ordinary breakpoint as a software breakpoint in writable RAM or a hardware
breakpoint in read-only memory. A debug-only profile does not need a serial
endpoint because `west debug` does not guarantee fresh application output.

The serial observer acknowledges arming after discarding queued input. Its
deadline includes the 180-second west operation budget plus the inventory's
serial observation timeout, so deployment does not consume the observation
allowance. Choose a pattern specific to the fixture application; a generic
banner alone cannot distinguish an older copy of that application still running.
This output oracle applies to `west flash`, which must start the new image; it is
not an acceptance condition for `debug`, `attach`, or `debugserver`.

Semihosting GDB commands must finish naturally (including detach/quit as
appropriate) within the configured timeout. The acceptance test requires a
successful west exit and matching console output. Generic interruption and
SSH-loss cleanup are exercised separately by the lifecycle tests; no separate
semihosting interruption sequence is assumed.
