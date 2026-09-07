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

The serial observer acknowledges arming after discarding queued input. Its
deadline includes the 180-second west operation budget plus the inventory's
serial observation timeout, so deployment does not consume the observation
allowance. Choose a pattern specific to the fixture application; a generic
banner alone cannot distinguish an older copy of that application still running.

Semihosting GDB commands must finish naturally (including detach/quit as
appropriate) within the configured timeout. The acceptance test requires a
successful west exit and matching console output. Generic interruption and
SSH-loss cleanup are exercised separately by the lifecycle tests; no separate
semihosting interruption sequence is assumed.
