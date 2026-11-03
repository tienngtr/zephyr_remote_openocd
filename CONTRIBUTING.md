# Contributing

This guide covers the development environment, the checks each change needs,
repository layout and code rules, commit and pull-request conventions, and the
rules for user documentation. The project targets Python 3.12 or newer and is a
self-contained Zephyr module.

## Set up the development environment

The product itself needs no pip installation: the Zephyr-configured Python
environment provides `pyelftools`, PyYAML, and jsonschema.
`requirements_dev.txt` contains contributor tooling only.

Create a virtual environment with the contributor tooling, so that
`.venv/bin/python` provides pytest, the static checks, and gitlint. Then install
gitlint's `commit-msg` hook, so that each commit message is checked against
gitlint's default rules when you commit:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements_dev.txt
.venv/bin/gitlint install-hook
```

If a `commit-msg` hook already exists, the installer leaves it in place and
reports an error. To use gitlint's hook instead, remove or rename the existing
hook and run `.venv/bin/gitlint install-hook` again.

## Validation checklist

For each change, run the checks listed for its type. In the pull request, name
the checks that ran and any applicable external tests that could not run.

| Change | Required checks |
| --- | --- |
| Every change | `.venv/bin/python -m pytest -n auto` and `.venv/bin/python scripts/contributor/static_check.py` |
| Zephyr runner or build integration | Applicable Zephyr integration tests against both Zephyr 4.4.x and 4.5.x |
| SSH, helper, session, or forwarding | Applicable SSH integration tests, when a suitable host is available |
| Flash, debug, RTT, semihosting, or other board-visible behavior | Affected hardware tests, when suitable hardware is available |

The Zephyr, SSH, and hardware checks are external tests and need more than the
virtual environment:

- **Zephyr integration tests:** a Zephyr 4.4.x or 4.5.x environment selected
  with `ZEPHYR_BASE`, but no hardware inventory. Covering both release families
  takes a run against each.
- **SSH integration and hardware tests:** a contributor-provided local hardware
  inventory.

Set these up as described in [Testing layers](docs/development/testing.md) and
[Hardware inventories](docs/development/hardware_inventories.md).

> [!WARNING]
> SSH and hardware tests can change external state, including flashing
> targets. Confirm the selection with `--collect-only`, run destructive
> profiles serially, and inspect cleanup output before reusing a target.

### Full external validation and releases

Before a release, or when claiming full external validation, run all applicable
external suites with `--require-external-tests`, so that a missing prerequisite
or skipped test fails the run, and inspect cleanup before reusing targets.

To release, tag the validated commit on `main` as `v` followed by the contents
of `VERSION`, and publish its GitHub release from that tag.

### Coverage (optional)

Collect branch coverage for production code, runner entry points, maintained
scripts, and local Python helper subprocesses with:

```sh
.venv/bin/python -m pytest -n auto --cov --cov-config=.coveragerc \
  --cov-report=term-missing
```

Coverage is informational and has no minimum threshold. CI publishes the
combined report as a Markdown job summary; see
[Coverage](docs/development/testing.md#coverage).

## Repository layout

| Path | Responsibility |
| --- | --- |
| `python/zephyr_remote_openocd/controller/` | Local sessions and transport |
| `python/zephyr_remote_openocd/helper/` | Remote execution |
| `python/zephyr_remote_openocd/protocol/` | Shared wire contract |
| `python/zephyr_remote_openocd/openocd/` | Command construction, without execution |
| `python/zephyr_remote_openocd/zephyr44/`, `zephyr45/` | Thin upstream compatibility adapters: `runner.py` public facades and `_runner.py` implementations |
| `python/zephyr_remote_openocd/_runner.py` | Shared remote operations |
| `python/zephyr_remote_openocd/_zephyr.py` | Active-adapter selection |
| `python/zephyr_remote_openocd/config.py` | User configuration loading |
| `python/zephyr_remote_openocd/resources/` | Configuration schema |
| `resources/config.example.yaml` | Canonical configuration template |
| `runners/remote_openocd.py` | West runner registration |
| `zephyr/` | Zephyr module integration |

Tests are organized by layer, not by package: `tests/unit/`,
`tests/local_integration/`, `tests/zephyr_integration/`,
`tests/ssh_integration/`, and `tests/hardware/`.

## Production code rules

- Keep production code under `python/zephyr_remote_openocd/`, free of board-
  and vendor-specific logic.
- Keep production dependencies minimal. Reuse dependencies already supplied by
  the supported Zephyr environment where appropriate instead of adding a
  project-specific installation path.
- Keep upstream OpenOCD coupling in the `zephyr44/` and `zephyr45/` adapters.
  Use only the public interface of Zephyr's `OpenOcdBinaryRunner`; do not
  access its private members.
- Change the configuration schema or Protocol v1 only with explicit
  authorization. Version changes must reflect contract compatibility
  decisions; do not create versions merely to record development history.
  - **Configuration schema:** update the schema, example configuration,
    loader, tests, requirements, and user documentation together, and state the
    compatibility impact and any required user updates.
  - **Protocol v1:** update the protocol document, client, helper, fixtures,
    requirements, and compatibility tests together.
- Validate configuration at runtime with `jsonschema` against the packaged
  schema, `python/zephyr_remote_openocd/resources/configuration.schema.json`.
  The schema is the only definition of the structural and lexical rules it
  expresses, so do not restate them in handwritten validators. Keep parser
  safeguards and checks that depend on runtime context in code.

### Environment variables

| Prefix | Use |
| --- | --- |
| `ZEPHYR_REMOTE_OPENOCD_` | Environment variables read by the product; for example, `ZEPHYR_REMOTE_OPENOCD_CONFIG` selects the product YAML file |
| `ZRO_` | Test harness, validation tools, test payloads, and generated internal markers; never a public product setting |

`ZRO_RECORD` and `ZRO_RECORD_OPENOCD_VERSION` control the runner's test-only
[recording mode](docs/development/testing.md#runner-recording-mode). External
tests select the local hardware inventory with the pytest option
`--hardware-inventory`, not an environment variable.

## Python API and encapsulation

Use three distinct boundaries:

- **Supported integration API:** the west runner entry point and the
  `RemoteOpenOcdBinaryRunner` facades in `zephyr44.runner` and
  `zephyr45.runner`. Configuration and Protocol v1 retain their separately
  documented contracts. Everything else, including unprefixed names,
  `controller.api`, and canonical internal imports, is package-internal and
  carries no external compatibility promise; the implementation package is not
  a general-purpose supported Python SDK.
- **Package-internal API:** put shared implementation in cohesive `_module.py`
  modules, and give intentionally consumed functions, classes, types, and
  methods normal names. `controller.api` curates the vocabulary for
  constructing and running sessions across implementation modules and tests.
  Domain modules and explicit `_module.py` modules remain the entry points for
  specific subsystem collaboration. Do not re-export implementation helpers
  from package initializers to make them appear public.
- **Module/class-private implementation:** use `_name` for local helpers and
  state, even within an internal module. Sibling modules should not import or
  access these names. Closely cooperating implementation classes within their
  defining module may access private state when that preserves one ownership
  boundary, such as registering a raw subprocess before its initializer can
  acquire resources.

Prefer renaming an existing cohesive module to `_module.py` over splitting it
into many small files.

Keep the root, `helper`, and `protocol` package initializers independent of
controller, configuration, OpenOCD planning, and Zephyr code, so that the
deployed helper runs with only the standard library. Check this for every
parent package whenever you add an import to an initializer or a facade.

### Canonical imports

Choose imports by the dependency being expressed:

| Dependency | Normal import location |
| --- | --- |
| Construct/run a controller session, consume its result or errors | `controller.api` (package-internal runtime facade) |
| Offline path/forward/staging planning | `controller.paths`, `controller.forwarding_spec`, `controller.staging_spec`, `controller.plan` |
| Specific controller collaboration | Its defining module, such as `controller.cancellation`, `controller.waiting`, or `controller._ssh` |
| Pure OpenOCD operation planning | `openocd.debug` or `openocd.flash`; shared command/template work stays in `openocd.command` or `openocd.arguments` |
| Shared protocol vocabulary | The domain module: `protocol.commands`, `protocol.responses`, `protocol.process`, `protocol.listeners`, `protocol.arguments`, `protocol.addresses`, `protocol.tcl`, `protocol.outcome`, or `protocol.wire` |
| Helper collaboration | Its defining `helper` module; no aggregate facade |
| Supported Zephyr integration | `zephyr44.runner` or `zephyr45.runner`; shared operation tests use `_runner` |

For example, session consumers use:

```python
from zephyr_remote_openocd.controller.api import (
    ForwardSpec,
    SessionController,
    SessionError,
    SessionPlan,
    SshCommand,
)
```

Controller collaborators import their dependencies from the defining modules;
never route them back through `controller.api`. Pure planners import the
specific offline descriptions without loading the runtime facade.

#### Adding a facade export

Curate a facade only where it decouples real callers from implementation
layout: add an export only when actual consumers share a coherent conceptual
vocabulary and the defining file is incidental to those consumers. A clear
domain module is already a canonical location; do not collect all its names
elsewhere. When adding an export:

- Keep explicit imports and `__all__`.
- Import definitions directly rather than chaining facades.
- Exclude ownership machinery and local helpers.
- Check the import graph against the package-initializer rule in
  [Python API and encapsulation](#python-api-and-encapsulation).

### Internal APIs in tests

Tests follow the same import rules: use `controller.api` for ordinary session
setup, and import an internal abstraction from its defining module when the
test exercises that abstraction through its normal interface. Module imports
used for precise fault injection also stay direct.

Prefer normal construction and effect-boundary fakes over private state
mutation. Narrow private hooks may remain for deterministic acquisition,
publication, reader-settlement, or cleanup fault injection when the precise
interleaving is the regression under test; document that reason at the hook.
See [test design](docs/development/test_design.md).

## Sensitive data

> [!IMPORTANT]
> Never put lab-specific data in commits, issues, pull requests, or their
> descriptions, comments, and reviews. This applies to logs and excerpts
> pasted into them too.

Lab-specific data includes:

- credentials, keys, and tokens;
- host names, addresses, and SSH host identities;
- probe serials and other capability values from a hardware inventory;
- serial-device and other device paths, and remote workspace paths;
- expected lab output and serial captures;
- populated hardware inventories.

Use placeholders in committed examples and in shared excerpts. Generated builds
and `.scratch/` files are local; never commit them.

## Commits and pull requests

- Keep each commit focused. Commit messages must pass gitlint's default rules,
  including a subject of at most 72 characters, a blank second line, a
  non-empty body, and body lines of at most 80 characters. Also use an
  imperative subject and a concise body that explains the change.
- In the pull request, describe the behavior change, the validation performed
  (see the [Validation checklist](#validation-checklist)), and fixture
  coverage.
- Update the SRS or SAD when a lifecycle, protocol, compatibility, or other
  architectural decision changes.

## User documentation

User documentation (`README.md`, `docs/user/`) describes the contract defined
by the SRS, SAD, Protocol v1, and configuration schema; it is not itself
authoritative. Resolve a questionable user-facing claim as follows:

1. If the sources agree that the claim is wrong, correct it.
2. If the sources disagree, report the conflict instead of choosing between
   them.
3. If the sources are silent, use production code as the evidence for current
   behavior, such as exact messages, printed output, and fixed limits. Describe
   that behavior as what the runner does, not as a guarantee.
4. If code conflicts with a formal requirement, report the conflict; code never
   overrides the requirement.

Write user documentation by these rules:

- **Canonical home.** Explain each concept once. Replace local text with a link
  only when the local task stays actionable without following it and the
  destination states the same claim, not a weaker or broader one.
- **Scope.** State restrictions with the scope the sources give them, and do
  not add consequences the sources do not state. For an unsupported case, name
  the component that gives no guarantee and the affected behavior; do not imply
  that the underlying tool forbids the action.
- **Reader address.** Prefer, in this order: an imperative ("Run the
  validator."); a technical subject ("The validator does not contact the
  remote host."); an explicit human subject when necessary ("Users of a custom
  wrapper must ensure that it preserves the stdin half-close."). Use second
  person only when avoiding it would make a sentence materially less clear or
  more awkward.
- **Order.** Open each section with the direct answer to its heading. State the
  rule before its exceptions, and keep back-references adjacent and explicit.
- **Wording.** Prefer the positive form when it is clearer or more direct;
  negative form suits unsupported cases and scoping limits. Use full,
  user-facing names ("the setup script", "OpenOCD commands") and standard
  phrasing ("does not start GDB"). Leave out incidental examples that imply a
  false distinction.
- **Examples.** Keep user documentation board- and vendor-agnostic. An
  example may name a concrete board that is publicly available in upstream
  Zephyr, provided the text makes clear that it is only an example and says
  what to replace it with.
- **Instructions.** Give a fix's purpose, and do not present it as the only or
  best fix without support. A substitution instruction says what to replace
  and with what. Phrase a heading as an instruction only when the section is a
  procedure.
- **Prerequisites.** State them before the task that needs them, and list
  only genuine prerequisites.
- **Visual structure.** Use headings, lists, tables, bold labels, and GitHub
  alerts when they improve scanability. Reserve alerts for consequences or
  constraints that readers are likely to miss. Do not use emoji as structural
  decoration.

## Further reading

- [Software Requirements Specification (SRS)](docs/requirements/SRS.md):
  behavior and requirements.
- [Software Architecture Document (SAD)](docs/architecture/SAD.md):
  architecture and rationale.
- [Helper protocol](docs/architecture/protocol.md): the helper wire contract.
- [Testing layers](docs/development/testing.md): test commands and
  external-test setup.
- [Hardware inventories](docs/development/hardware_inventories.md): local
  inventory files for SSH integration and hardware tests.
- [Test design](docs/development/test_design.md): guidelines for writing tests.
- [Assurance methods](docs/development/assurance_methods.md): bounded
  exploratory campaigns beyond the maintained suite.
