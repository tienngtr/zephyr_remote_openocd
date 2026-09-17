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
coherent validated commits with gitlint-compliant messages. Review the complete
diff, exclude unrelated changes, and never rewrite history unless authorized.

Never commit credentials, hosts, device paths, capability values, expected lab
output, populated inventories, generated builds, or `.scratch/` artifacts. Use
placeholders in committed examples.

## Subagent Review Gate

When a review produces multiple actionable findings, process them sequentially
because agents share a worktree. Before starting the fix loop or switching the
primary model, preserve the full review in
`.scratch/agents/review-findings.md` while its context is fresh. For every
finding record its severity, behavioral contract or invariant, relevant
production execution path and evidence, a concrete failure scenario,
constraints or non-goals, acceptance criteria, and focused validation.

For each finding:

1. Extract a focused, self-contained finding packet under `.scratch/agents/`.
   It must retain the contract, production evidence, failure scenario,
   constraints, acceptance criteria, and validation—not a terse summary or the
   full history of a long thread.
2. Spawn one `finding_worker` with `fork_turns="none"` and give it that packet.
   Assign exactly one bounded finding. Keep its changes uncommitted until the
   finding is independently accepted.
3. The primary agent must inspect the complete diff, trace the relevant
   production path, and determine whether tests can pass for the wrong reason.
   The worker's tests and self-assessment are not sufficient approval. If the
   user authorized automatic commits, commit the accepted finding as a coherent
   validated milestone before starting another.
4. If the primary agent or risk reviewer finds a concrete defect, return the
   exact counterexample, violated invariant, or required property to the same
   worker and allow one bounded informed revision. Do not merely ask it to try
   again.

The primary agent takes over implementation when that informed revision still
fails, the behavioral contract is ambiguous, the required change crosses into
architectural work, the worker repeats the failed approach or reports a
blocker, or continuing would be unsafe.

A `wait_agent` timeout is not evidence that a worker is stuck. Prefer an
initial wait of several minutes for implementation or validation work rather
than polling every few seconds; use shorter waits only for an explicit user
status request, a known short command, or evidence of a blocker. Send at most
one checkpoint request during a normal wait interval. Before any
interruption, the primary agent SHALL inspect `git status` and the focused diff
for recent shared-worktree progress, then request a concise checkpoint. Do not
interrupt merely because waits expire, the user reduces remaining scope, or
work takes several minutes while progress is visible. Interrupt only when the
user asks for immediate termination, the worker clearly drifts, its direction
is unsafe or materially wrong, it is blocked or repeating failure, or the
primary agent is taking over under the rule above. Record the concrete reason
for interrupting in the handoff. If it appears nearly complete, ask it to stop
making changes and return its handoff, then wait for normal completion.

Also spawn a `risk_reviewer` with `fork_turns="none"` when a change affects
cleanup or rollback, locks, processes, signals, sockets, concurrency or timing,
protocol or parser boundaries, path safety, destructive hardware behavior, or
code that can silently produce false validation evidence. Use it as well for
ambiguity found during primary review or changes crossing architectural layers.
Provide only the focused packet, compact diff, focused test evidence, and
directly relevant execution paths. Treat the reviewer as logically read-only;
verify that it did not modify the shared worktree before using its findings.
Spawn a fresh reviewer for each finding so its context is limited to that
finding. Reuse that reviewer only for bounded follow-up review of the same
finding after a concrete revision; never reuse it for another finding.

Do not end a primary-agent turn, present a finding as complete, or start the
next finding while its worker or risk-review gate is active. Report the gate as
pending and resume it first. If a user asks only for status while a gate is
active, answer the status question and continue the active gate rather than
implicitly ending the task.

For lifecycle work, review operation and nested-cleanup success/failure,
partial/absent resources, primary-exception preservation, and retryability. For
protocol work, review malformed bytes/syntax/envelopes/fields and valid messages
that conflict with local state.

After all accepted findings are complete, perform an aggregate review of the
cumulative changes using the same standard as the initial review. Retain the
master and per-finding handoffs until it passes so reopened findings and
cross-finding regressions retain their context. The primary agent receiving the
successful final review removes the session handoffs.
