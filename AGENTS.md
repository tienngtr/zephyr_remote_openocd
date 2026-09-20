# Repository Guidelines

## Scope and References

Use Python 3.12+. Production code lives in `python/zephyr_remote_openocd/`:
board-independent logic in `remote/`, Zephyr 4.4 integration in
`zephyr44/runner.py`, and the west entry point in `runners/remote_openocd.py`.
Requirements and architecture live in `docs/requirements/SRS.md` and
`docs/architecture/SAD.md`.

Follow `CONTRIBUTING.md` for setup, naming, environment variables, and ordinary
checks. Before external validation, follow `docs/development/testing.md` and
`docs/development/hardware_inventories.md`. Tests live under `tests/unit/`,
`tests/zephyr_integration/`, `tests/ssh_integration/`, and `tests/hardware/`;
the last two require external resources. External tests can build and flash:
collect nodes first, run destructive profiles serially, and inspect cleanup.

Keep builds, populated hardware inventories, and temporary agent notes in
ignored `.scratch/`. Put real equipment data in `.scratch/hardware/` and agent
handoffs in `.scratch/agents/`.

## Implementation and Contracts

Use four-space indentation, type hints, `snake_case` functions/modules,
`PascalCase` classes, uppercase constants, immutable transport dataclasses, and
`pathlib`. Minimize production dependencies.

Keep production board- and vendor-independent. Isolate Zephyr 4.4 coupling in
its adapter, preserve the public runner boundary, and never use private
`OpenOcdBinaryRunner` members. Use the configured `SshCommand` for every remote
action; never assume system `ssh` or hard-code test or lab host details.
Preserve Zephyr command behavior and enabled GDB, Tcl, and telnet services.
Bind remotely to the allocated loopback and locally to `127.0.0.1`; clean up on
success, failure, interruption, and SSH loss.

The product YAML contract is
`python/zephyr_remote_openocd/resources/configuration.schema.json`, with
`resources/config.example.yaml` as its canonical template; unknown keys are
errors. Do not add, remove, or relax keys without explicit authorization. An
authorized change must update the schema, example, loader, tests, requirements,
and user docs together and state compatibility and migration. Hardware
inventories have a separate schema. Never create schema versions merely to
record development history.

Protocol v1 is fixed during development and requires full-contract support,
not version equality alone. Change it only with explicit authorization, then
update the protocol document, client, helper, fixtures, requirements, and
compatibility tests together. Do not version development history. Helper
stdout is JSON-only; relay child output as events.

RTT uses structured runner state and its configured port, never inferred GDB
RSP traffic. Direct semihosting validation uses fixture-provided OpenOCD
`--cmd-pre-init` commands and normal stdout/stderr relay—no proxy, filesystem
virtualization, or GDB File-I/O. Serial observation is test-only.

Let invalid input fail naturally when that is sufficient; do not add redundant
error checks.

## Testing and Validation

Use pytest `test_` names and distinct parametrized nodes for external profiles.
Cover affected contracts and plausible failures at the smallest useful layer,
especially protocol ordering, lifecycle failure, cleanup, path safety, and
command construction. Recording mode (`ZRO_RECORD=1`) must perform no SSH,
OpenOCD, GDB, or hardware I/O. Thread-info tests inject a version only in
no-I/O modes; production queries configured remote OpenOCD.

Add or strengthen regression coverage only when it materially improves
confidence in affected behavior or a contract. Test meaningful behavior,
regressions, and plausible failure modes at the smallest useful layer; do not
add a test mechanically for each changed line or helper. Avoid redundant tests
that restate coverage already provided at the same boundary.

When practical, verify that new or materially changed regression coverage
detects the pre-fix behavior. A passing test is not useful evidence if its mock,
fake, fixture, or setup bypasses the production path that contained the defect.
Trace that boundary and repair coverage that passes for the wrong reason rather
than adding more tests around the same faulty setup.

Prefer semantic assertions over incidental implementation details. A
behavior-preserving refactor should normally not require test changes. Assert
exact prose, serialized bytes, command formatting, internal call order, tool
flags, or private structure only when that exact form is a required interface or
compatibility contract. Do not make assertions broader or more exact merely to
increase coverage.

Concurrency tests require explicit, deterministic synchronization. Do not use
`time.sleep()`, elapsed-time assertions, retries, shortened production
timeouts, or scheduling assumptions to coordinate behavior. Use observable
handshakes—events, barriers, pipes, socket readiness, process exit, controlled
fakes, or monkeypatched clocks—and assert state, calls, events, or cleanup
order. Test timeouts are generous deadlock safety nets only; successful tests
must not depend on expiry. Real-process tests may poll an explicit condition
with a generous overall deadline, but must not assert normal completion timing.
Do not add production configuration solely to make a test convenient; adapt the
test to the production interface unless configurability is independently needed.

Do not unit-test repository tooling merely because it contains code. Static
check wrappers, lint configuration, test helpers, and orchestration normally
prove themselves by being run. Test them only when non-trivial logic could
silently invalidate validation, safety, cleanup, or external-test results.

Use `.venv/bin/python` for repository pytest and developer tools when it exists.
If it is absent or unusable, set it up per `CONTRIBUTING.md`; do not fall back to
system Python merely for a missing import. This environment may drive external
pytest, but direct Zephyr/west operations must use the selected layer's
configured checkout, west, SDK, toolchain, and environment. Consult
`docs/development/testing.md` when uncertain.

Run validation proportional to the change and always run
`.venv/bin/python scripts/static_check.py` for repository static checks. It
uses single-process Pylint in restricted sandboxes. Use `--collect-only` before
external nodes and inspect cleanup before target reuse. See `CONTRIBUTING.md`
for the full checklist.

## Work and Git

Follow task-specific `.agents/GOAL.md` and `.agents/PLAN.md` when present.
Durable requirements and architectural decisions belong in tracked docs;
temporary investigations belong in `.scratch/agents/`.

Commit, push, or create/update a pull request only with explicit authorization;
pushing and PR actions always need separate authorization. If authorized, make
coherent validated commits with gitlint-compliant messages: use an imperative
subject of at most 72 characters, a blank line, and a concise body explaining
the change with lines of at most 80 characters. Review the complete diff,
exclude unrelated changes, and never rewrite history unless authorized.

Never commit credentials, hosts, device paths, capability values, expected lab
output, populated inventories, generated builds, or `.scratch/` artifacts. Use
placeholders in committed examples.
