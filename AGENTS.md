# Repository Guidelines

## Project Structure & Module Organization

Production Python is under `python/zephyr_remote_openocd/`: board-independent
logic in `remote/`, Zephyr coupling in `zephyr44/runner.py`, and the west entry
point in `runners/remote_openocd.py`. Requirements and architecture live in
`docs/requirements/SRS.md` and `docs/architecture/SAD.md`. Tests are split across `tests/unit/`,
`tests/zephyr_integration/`, `tests/ssh_integration/`, and fixture-gated
`tests/hardware/`. Generated builds and fixture configuration belong in ignored
`.scratch/`.

## Build, Test, and Development Commands

Use Python 3.12 or newer. Run the self-contained suite with:

```sh
pytest
```

Initialize per-user setup (without editing repositories or shell files) with:

```sh
python3 scripts/setup.py
```

Run external layers explicitly with:

```sh
pytest tests/zephyr_integration -m zephyr
pytest tests/ssh_integration -m ssh --hardware-config /path/to/hardware.toml
pytest tests/hardware -m hardware --hardware-config /path/to/hardware.toml
```

For serial native-Linux release validation, use
`python3 scripts/release_validate.py` with the inventory and Zephyr arguments
documented in `docs/development/testing.md`. It is intentionally separate from
ordinary pytest discovery.

Zephyr-style Python checks are configured in `.ruff.toml` and `pylintrc`:
`ruff check .`, `ruff format --check .`, `pylint --rcfile=pylintrc $(git ls-files
'*.py')`, and `vermin -p=1 -f parsable --violations -t=3.12-
--no-make-paths-absolute $(git ls-files '*.py')` (single-process mode is useful
in restricted sandboxes).

External tests use the ignored TOML inventory selected by `--hardware-config`
or `ZRO_HARDWARE_CONFIG`; read `docs/development/hardware-fixtures.md` before
hardware tests. They may build and flash targets, so inspect nodes with
`--collect-only` first. Finish with `git diff --check`.

## Coding Style & Naming Conventions

Follow standard Python style: four-space indentation, `snake_case` functions and
modules, `PascalCase` classes, and uppercase constants. Prefer type hints,
immutable dataclasses for transport models, and `pathlib` paths. Production code
should minimize additional third-party dependencies; tests may use established
developer-only libraries when they materially improve structure or diagnostics.
Keep production behavior board/vendor-agnostic. Preserve the public Zephyr
runner boundary; never access private `OpenOcdBinaryRunner` members.

## Architecture Invariants

Use configured `SshCommand` for every remote action; never embed fixture hosts or
assume system `ssh`. Preserve Zephyr 4.4 command semantics and all enabled GDB,
Tcl, and telnet services. Bind remotely to the allocated loopback and locally to
`127.0.0.1`. Preserve cleanup on success, failure, interruption, and SSH loss.
The enumerated V1 TOML schema is frozen; do not add or relax configuration keys
without an explicit compatibility and migration decision. Unknown keys remain
errors, and `resources/config.toml.example` is the canonical template.

Protocol 1 is frozen: matching version numbers alone are insufficient unless
both sides implement the canonical Protocol 1 contract. Any incompatible change or
new client behavior requiring helper support requires protocol 2. Keep helper
stdout JSON-only and relay child output as events. Serial observation is test-only.
RTT uses structured runner state and the configured port; never infer it from
GDB RSP traffic. Direct semihosting console validation uses fixture-supplied
OpenOCD `--cmd-pre-init` commands and the normal stdout/stderr relay. It adds
no semihosting proxy, filesystem virtualization, or GDB File-I/O path; target
capabilities and commands for real lab equipment stay in ignored hardware
fixtures.

## Testing Guidelines

Tests use pytest; files and functions/classes follow `test_` naming and external
profiles are parametrized as separate nodes. Add focused tests for protocol
ordering, lifecycle failures, cleanup, path safety, and command construction.
Recording mode (`ZEPHYR_REMOTE_OPENOCD_RECORD=1`) must remain
strictly free of SSH, OpenOCD, GDB, and hardware I/O. Concrete lab identities,
serial devices, capability values, and expected output stay in uncommitted
fixture files; committed examples must use neutral placeholders. Thread-info
tests use an injected version in no-I/O modes; production must query the
configured OpenOCD executable remotely.

## Contributor Playbooks

For an ordinary change, run `pytest`, Ruff, and `git diff --check` before
committing. For external validation, copy the neutral inventory template,
run `pytest --collect-only` with `--hardware-config`, then select the desired
Zephyr, SSH, or hardware layer explicitly. For native-Linux release evidence,
run `scripts/release_validate.py` with the configured inventory, Zephyr tree,
west, board, and benchmark arguments. Keep destructive profiles serial and
inspect cleanup output before reusing a target.

## Work Tracking

The repository may not contain `.agents/GOAL.md` or `.agents/PLAN.md`; they were
temporary work-tracking documents and are not required for normal development.
If a future task supplies either file, follow its current instructions for that
task only. Temporary investigations and handoff notes belong under ignored
`.scratch/agents/`, never as the sole record of a requirement, architecture
decision, or release result.

## Commit & Push Guidelines

Create commits while pursuing this goal.
Commit coherent, validated milestones. Review the complete diff.
Exclude unrelated changes. Run relevant validation first.
Avoid arbitrary checkpoint commits.
Do not rewrite history unless explicitly authorized.
Use concise imperative subjects.
Never commit credentials, hosts, device paths, or `.scratch/` artifacts. Do not
push unless the user explicitly requests it.
