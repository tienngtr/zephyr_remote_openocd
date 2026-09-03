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

Use Python 3.12 or newer. Run the hardware-free suite with:

```sh
PYTHONPATH=python python3 -m unittest discover -s tests/unit -v
```

Initialize per-user setup (without editing repositories or shell files) with:

```sh
python3 scripts/setup.py
```

Run all locally available tests with:

```sh
PYTHONPATH=python python3 -m unittest discover -s tests -p 'test_*.py'
```

Zephyr-style Python checks are configured in `.ruff.toml` and `pylintrc`:
`ruff check .`, `ruff format --check .`, `pylint --rcfile=pylintrc $(git ls-files
'*.py')`, and `vermin -f parsable --violations -t=3.12
--no-make-paths-absolute $(git ls-files '*.py')`.

Zephyr integration uses `ZEPHYR_BASE`, `WEST`, and `OPENOCD_TEST_BOARD`; SSH
tests use `ZRO_SSH_TEST_HOST`. Read `tests/README.md` before hardware tests;
they flash targets and require external fixture files. Finish with
`git diff --check`.

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

Tests use `unittest`; files and methods start with `test_`. Add focused unit tests
for protocol ordering, lifecycle failures, cleanup, path safety, and command
construction. Recording mode (`ZEPHYR_REMOTE_OPENOCD_RECORD=1`) must remain
strictly free of SSH, OpenOCD, GDB, and hardware I/O. Concrete lab identities,
serial devices, capability values, and expected output stay in uncommitted
fixture files; committed examples must use neutral placeholders. Thread-info
tests use an injected version in no-I/O modes; production must query the
configured OpenOCD executable remotely.

## Work Tracking

For the repository-comprehension refactor, treat `.agents/GOAL.md` as the stable
project contract and `.agents/PLAN.md` as the status-tracked implementation
strategy. Change the goal only when the user changes scope or acceptance; update
plan statuses and findings as work proceeds. Reconcile both files before each
semantic commit. Temporary investigations and handoff notes belong under ignored
`.scratch/agents/`, never as the sole record of a requirement, architecture
decision, or release result.

## Commit & Push Guidelines

Create commits while pursuing this goal.
Commit coherent, validated milestones. Review the complete diff.
Exclude unrelated changes. Run relevant validation first.
Avoid arbitrary checkpoint commits.
Do not rewrite history unless explicitly authorized.
Use concise imperative subjects.
Never commit credentials, hosts, device paths, or `.scratch/` artifacts.
Push after each commit.
