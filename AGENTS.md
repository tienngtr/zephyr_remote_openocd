# Repository Guidelines

## Scope and Layout

Use Python 3.12 or newer. Production code is under
`python/zephyr_remote_openocd/`: board-independent logic in `remote/`, Zephyr
4.4 coupling in `zephyr44/runner.py`, and the west entry point in
`runners/remote_openocd.py`. Requirements and architecture are in
`docs/requirements/SRS.md` and `docs/architecture/SAD.md`.

Tests are divided among `tests/unit/`, `tests/zephyr_integration/`,
`tests/ssh_integration/`, and `tests/hardware/`; the last two require external
test resources. Keep generated builds, populated hardware inventories, and
temporary agent notes under `.scratch/`, which is excluded from version control.

Follow `CONTRIBUTING.md` for setup, naming, environment-variable prefixes, and
ordinary checks. Follow `docs/development/testing.md` and
`docs/development/hardware_inventories.md` before external or hardware validation.
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
`resources/config.example.yaml` is its canonical template. Unknown keys are
errors. Do not add, remove, or relax keys without explicit authorization. An
authorized contract change must update the schema, example, loader, tests,
requirements, and user documentation together and state its compatibility and
migration decision. Do not create a schema version to record development
history. Hardware inventories use their separate YAML schema.

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
test-only. Real equipment capabilities and commands belong in local hardware
inventories under `.scratch/config/`.

## Test Rules

Use pytest `test_` names and separate parametrized nodes for external profiles.
Add focused coverage for protocol ordering, lifecycle failures, cleanup, path
safety, and command construction. Recording mode (`ZRO_RECORD=1`) must perform
no SSH, OpenOCD, GDB, or hardware I/O.

When `.venv/bin/python` exists, use it for repository-maintained pytest and
developer tooling, including pytest drivers for external test layers. Do not
substitute the repository virtual environment for an environment required by
the system under test: direct Zephyr or west operations must use the configured
Zephyr environment, checkout, west executable, SDK, and toolchain as required
by the selected validation layer. If the repository virtual environment is
absent or unusable, follow `CONTRIBUTING.md` for setup instead of falling back
to a system Python merely because an import is missing. Follow the layer-specific
commands in `docs/development/testing.md` when the required environment is
unclear.

Add or strengthen regression coverage only when it materially improves
confidence in an affected behavior or contract. Test meaningful behavior,
contracts, regressions, and plausible failure modes at the smallest useful
layer. When practical, verify that new or materially changed regression
coverage detects the pre-fix behavior, but do not add brittle or redundant
tests solely to demonstrate red-green development. Check that fakes and mocks
exercise the production boundary they claim to cover; coverage that passes for
the wrong reason should be repaired rather than supplemented mechanically.

Prefer semantic assertions over incidental details. A behavior-preserving
refactor should normally not require test changes. Assert exact prose,
serialized bytes, command formatting, internal call order, or tool flags only
when that exact form is itself a required interface or compatibility contract.

Do not unit-test repository tooling merely because it has code. Static-check
wrappers, lint configuration, test helpers, and test orchestration normally
prove themselves by being run; test them only when non-trivial logic could
silently invalidate validation, safety, cleanup, or external-test results.

Thread-info tests inject a version only in no-I/O modes; production queries the
configured remote OpenOCD executable.

Run validation appropriate to the change and use `.venv/bin/python
scripts/static_check.py` for all repository static checks. The script uses
single-process Pylint for restricted sandboxes. Inspect external nodes with
`--collect-only`, and inspect cleanup output before reusing a target. See
`CONTRIBUTING.md` for the contributor validation checklist.

## Work and Git

Durable rules belong here. Follow task-specific `.agents/GOAL.md` and
`.agents/PLAN.md` when present. Put temporary investigations and handoffs under
untracked `.scratch/agents/`; put requirements, architecture decisions, and
other durable project decisions in their tracked documents.

Commit, push, or create/update a pull request only with explicit user
authorization. For long tasks, suggest authorization for automatic commits when
useful; pushing and pull requests always require separate authorization. Commit
coherent, validated milestones with informative messages that pass default
gitlint rules. Review the complete diff, exclude unrelated changes, and do not
rewrite history unless authorized.

Never commit credentials, hosts, device paths, capability values, expected lab
output, populated inventories, generated builds, or `.scratch/` artifacts.
Committed examples use placeholders.

## Subagent Quality Gate

When a review produces multiple actionable findings, process them sequentially
because agents share the worktree. Before starting the fix loop or switching the
primary model, preserve the detailed review context under
`.scratch/agents/review-findings.md` while it is fresh. Each actionable finding
should record its severity, behavioral contract or invariant, relevant
production execution path and evidence, a concrete failure scenario,
constraints or non-goals, acceptance criteria, and focused validation.

Use `finding_worker` for one bounded finding at a time and keep its changes
uncommitted until the finding is accepted. Spawn `finding_worker` and
`risk_reviewer` with `fork_turns="none"`; give each a focused, self-contained
finding packet under `.scratch/agents/` rather than a terse summary or the full
history of a long-running thread. The focused packet may be extracted from the
master review handoff, but must preserve the material contract, evidence,
failure scenario, constraints, and acceptance criteria.

After each implementation, the primary agent must independently inspect the
complete diff, trace the relevant production path, and assess whether tests can
pass for the wrong reason. A worker's passing tests and self-assessment are not
sufficient approval to commit. When the user has explicitly authorized
automatic commits, commit each accepted finding as a coherent validated
milestone before starting the next one.

If the primary agent or risk reviewer finds a concrete defect in a worker's
solution, send the exact counterexample, violated invariant, or required
property back to the same worker and allow one bounded revision before taking
over implementation. Do not simply ask the worker to try again without new
information. Escalate implementation to the primary agent when an informed
revision still fails, the behavioral contract is ambiguous, the required change
crosses the bounded finding into architectural work, the worker is repeating
the same failed approach or reports a blocker, or continuing the current
approach would be unsafe.

A `wait_agent` timeout is not evidence that a worker is stuck. Do not interrupt
an agent merely because one or more waits time out, because a user reduces the
remaining scope, or because the current finding is taking several minutes while
the agent is making observable progress. Interrupt only when the user requests
immediate termination, the agent is clearly drifting, the current direction is
unsafe or materially wrong and should not continue, the agent is blocked or
repeating a failed operation, or the primary agent is intentionally taking over
under the escalation rule above. If an agent appears nearly complete, ask it to
make no further changes and return its handoff, then wait for normal completion
instead of interrupting it for status.

Also use `risk_reviewer` when a change affects cleanup or rollback, locks,
processes, signals, sockets, concurrency or timing, protocol or parser
boundaries, path safety, destructive hardware behavior, or code that can
silently produce false validation evidence. Also escalate when the primary
review finds ambiguity or when a change crosses multiple architectural layers.
Give the risk reviewer only the focused finding packet, compact diff, focused
test evidence, and directly relevant execution paths. Treat the reviewer as
logically read-only even when its role requests a read-only sandbox; do not rely
on sandbox inheritance as the enforcement boundary. The primary agent must
verify that the reviewer did not modify the shared worktree before using its
findings.

For lifecycle changes, review the combinations of operation success or
failure, inner and outer cleanup success or failure, partial or absent resource
state, primary exception preservation, and retryability. For protocol
boundaries, consider malformed bytes, malformed syntax, invalid envelopes,
invalid semantic fields, and valid messages that conflict with local state.

After all accepted findings are complete, perform an aggregate final review of
the cumulative changes using the same strong review standard as the initial
review. Retain the master and per-finding handoffs until that final review is
accepted so they remain available for cross-finding regressions or reopened
work. The primary agent that receives the successful final-review result owns
removing the session handoffs under `.scratch/agents/`.
