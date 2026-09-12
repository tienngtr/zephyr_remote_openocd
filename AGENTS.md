# Repository Guidelines

## Scope and Layout

Use Python 3.12 or newer. Production code is under
`python/zephyr_remote_openocd/`: board-independent logic in `remote/`, Zephyr
4.4 coupling in `zephyr44/runner.py`, and the west entry point in
`runners/remote_openocd.py`. Requirements and architecture are in
`docs/requirements/SRS.md` and `docs/architecture/SAD.md`.

Tests are divided among `tests/unit/`, `tests/zephyr_integration/`,
`tests/ssh_integration/`, and `tests/hardware/`; the last two require external
fixtures. Keep generated builds, populated fixtures, and temporary agent notes
in ignored `.scratch/` paths.

Follow `CONTRIBUTING.md` for setup, naming, environment-variable prefixes, and
ordinary checks. Follow `docs/development/testing.md` and
`docs/development/hardware_fixtures.md` before external or hardware validation.
External tests can build and flash targets, so collect nodes before execution
and keep destructive profiles serial.

## Implementation Rules

Use four-space indentation, type hints, `snake_case` functions and modules,
`PascalCase` classes, uppercase constants, immutable transport dataclasses, and
`pathlib` paths. Minimize production dependencies; tests may use established
developer tools when they improve diagnostics or structure.

Keep production behavior board- and vendor-independent. Confine Zephyr 4.4
coupling to its adapter, preserve the public runner boundary, and never access
private `OpenOcdBinaryRunner` members.

Use the configured `SshCommand` for every remote action; do not assume system
`ssh` or embed fixture hosts. Preserve Zephyr command behavior and all enabled
GDB, Tcl, and telnet services. Bind remotely to the allocated loopback and
locally to `127.0.0.1`. Clean up after success, failure, interruption, and SSH
loss.

## Configuration and Protocol

`python/zephyr_remote_openocd/resources/configuration.schema.json` defines the YAML contract, and
`resources/config.yaml.example` is its canonical template. Unknown keys are
errors. Do not add, remove, or relax keys without explicit authorization. An
authorized contract change must update the schema, example, loader, tests,
requirements, and user documentation together and state its compatibility and
migration decision. Do not create a schema version to record development
history. Hardware fixture inventories may remain TOML.

Protocol v1 is the current wire contract. Version equality is insufficient
unless both peers implement the complete contract. Change it only with explicit
authorization, then update the protocol document, client, helper, fixtures,
requirements, and compatibility tests together. Its wire value and contract
remain fixed during this development phase; do not create another version to
record history. Keep helper stdout JSON-only and relay child output as events.

RTT must use structured runner state and its configured port, never inferred
GDB RSP traffic. Direct semihosting validation uses fixture-supplied OpenOCD
`--cmd-pre-init` commands and normal stdout/stderr relay; it adds no proxy,
filesystem virtualization, or GDB File-I/O path. Serial observation is
test-only. Real equipment capabilities and commands belong in ignored fixture
files.

## Test Rules

Use pytest `test_` names and separate parametrized nodes for external profiles.
Add focused coverage for protocol ordering, lifecycle failures, cleanup, path
safety, and command construction. Recording mode (`ZRO_RECORD=1`) must perform
no SSH, OpenOCD, GDB, or hardware I/O.

Never commit lab identities, serial devices, capability values, expected lab
output, or populated inventories; committed examples use placeholders.
Thread-info tests inject a version only in no-I/O modes; production queries the
configured remote OpenOCD executable.

Run validation appropriate to the change and use `python3
scripts/static_check.py` for all repository static checks. The script uses
single-process Pylint for restricted sandboxes.
Inspect external nodes with `--collect-only`, and inspect cleanup output before
reusing a target. `scripts/release_validate.py` is the separate serial
native-Linux release driver described in `docs/validation/README.md`.

## Work and Git

Durable rules belong here. Follow task-specific `.agents/GOAL.md` and
`.agents/PLAN.md` when present. Put temporary investigations and handoffs under
ignored `.scratch/agents/`; put requirements, architecture decisions, and
release results in their tracked documents.

Commit, push, or create/update a pull request only with explicit user
authorization. For long tasks, suggest authorization for automatic commits when
useful; pushing and pull requests always require separate authorization. Commit
coherent, validated milestones with informative messages that pass default
gitlint rules. Review the complete diff, exclude unrelated changes, and do not
rewrite history unless authorized. Never commit credentials, hosts, device
paths, generated builds, or `.scratch/` artifacts.
