# Contributing

Use Python 3.12 or newer and keep the repository as a self-contained Zephyr
module. Product use does not require pip; the Zephyr-configured Python
environment provides `pyelftools`, PyYAML, and jsonschema. Module developers may
install the convenience tooling in `requirements_dev.txt`; normal users should
not install that file.

Keep production dependencies minimal. Reuse dependencies already supplied by
the supported Zephyr environment where appropriate instead of adding a
project-specific installation path.

## Validation checklist

Run the checks appropriate to every change and state the actual coverage in the
pull request:

- Every change: run `.venv/bin/python -m pytest` and `.venv/bin/python scripts/contributor/static_check.py`.
- Zephyr runner or build-integration changes: run the applicable Zephyr 4.4.x
  integration tests.
- SSH, helper, session, or forwarding changes: run the applicable SSH
  integration tests when a suitable host is available.
- Flash, debug, RTT, semihosting, or other board-visible changes: run the
  affected hardware tests when suitable hardware is available.
- Before a release/tag, or when claiming full external validation, run all
  applicable external suites with `--require-external-tests`, confirm there are
  no unexpected skips, and inspect cleanup before reusing targets. Tag the
  release commit on `main` as `v` followed by the contents of `VERSION`, and
  publish its GitHub release from that tag.
- PR descriptions should state what validation was performed and explicitly
  mention relevant external tests that could not be run.
- Never put credentials, SSH host identities, probe serials, serial-device
  paths, populated hardware inventories, or other lab-specific sensitive data
  in pull requests or committed files.

Set up the contributor environment and run the ordinary checks with:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements_dev.txt
.venv/bin/python -m pytest
.venv/bin/python scripts/contributor/static_check.py
```

Collect branch coverage for production code, runner entry points, maintained
scripts, and local Python helper subprocesses with:

```sh
.venv/bin/python -m pytest --cov --cov-config=.coveragerc \
  --cov-report=term-missing
```

CI additionally publishes a Markdown job summary. Coverage is informational and
has no minimum threshold.

Under `python/zephyr_remote_openocd/`, `controller/` owns local sessions and
transport, `helper/` executes remotely, `protocol/` defines the shared wire
contract, and `openocd/` constructs commands without executing them.
the `zephyr44/` package contains Zephyr 4.4.x integration, with `runner.py` as
its public compatibility facade and `_runner.py` as the implementation,
`runners/remote_openocd.py` registers the west runner, and `resources/` contains
the user configuration contract. Matching tests live under `tests/unit/`,
`tests/local_integration/`, `tests/zephyr_integration/`,
`tests/ssh_integration/`, and `tests/hardware/`.

Production code belongs under `python/zephyr_remote_openocd/` and must remain
board/vendor-agnostic. Keep Zephyr 4.4.x-specific coupling in the `zephyr44/` package,
use only the supported runner interface, and do not access private
`OpenOcdBinaryRunner` members. Do not change the configuration schema or
Protocol v1 unless the work explicitly requires it. When either changes, update
its specification, implementation, examples, and tests together. Version
changes must reflect contract compatibility decisions.

The packaged configuration schema is the sole source for the structural and
lexical rules it expresses. Runtime configuration validation must use
`jsonschema` against that schema; do not duplicate those rules in handwritten
validators. Parser safeguards and contextual semantic checks remain separate
from schema-defined validation.

## Python API and encapsulation

Use three distinct boundaries:

- **Supported integration API:** the west runner entry point and the curated
  `zephyr44.runner.RemoteOpenOcdBinaryRunner` facade. Configuration and Protocol
  v1 retain their separately documented contracts; the implementation package
  is not a general-purpose supported Python SDK.
- **Package-internal API:** shared implementation lives in cohesive `_module.py`
  modules, with normal names for intentionally consumed functions, classes,
  types, and methods. `controller.api` curates the vocabulary for constructing
  and running sessions across implementation modules and tests. It is an
  internal convenience, with no external compatibility promise. Domain modules
  and explicit `_modules` remain the entry points for specific subsystem
  collaboration; an unprefixed name alone does not promise external
  compatibility. Do not re-export implementation helpers from package
  initializers to make them appear public.
- **Module/class-private implementation:** use `_name` for local helpers and
  state. Sibling modules should not import or access these names. Closely
  cooperating implementation classes within their defining module may access
  private state when that preserves one ownership boundary, such as registering
  a raw subprocess before its initializer can acquire resources.

Prefer renaming an existing cohesive module to `_module.py` over splitting it
into many small files. Keep module-local helpers private even within an internal
module. Curate facades only where they decouple real callers from implementation
layout; keep package initializers light so helper deployment stays independent
of controller and Zephyr imports.

### Canonical imports

Choose imports by the dependency being expressed:

| Dependency | Normal import location |
| --- | --- |
| Construct/run a controller session, consume its result or errors | `controller.api` (package-internal runtime facade) |
| Offline path/forward/staging planning | `controller.paths`, `controller.forwarding_spec`, `controller.staging_spec`, `controller.plan` |
| Specific controller collaboration | Its defining module, such as `controller.cancellation`, `controller.waiting`, or `controller._ssh` |
| Pure OpenOCD operation planning | `openocd.debug` or `openocd.flash`; shared command/template work stays in `openocd.command` or `openocd.arguments` |
| Shared protocol vocabulary | The domain module: `protocol.commands`, `protocol.responses`, `protocol.process`, `protocol.listeners`, `protocol.arguments`, `protocol.outcome`, or `protocol.wire` |
| Helper collaboration | Its defining `helper` module; no aggregate facade |
| Supported Zephyr integration | `zephyr44.runner`; adapter implementation tests may use `zephyr44._runner` |

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

Controller collaborators continue importing their dependencies directly; never
route them back through `controller.api`. Pure planners should import the
specific offline descriptions without loading the runtime facade. Tests follow
the same rule: use the facade for ordinary session setup, but import an explicit
internal abstraction directly when that is what the test exercises. Module
imports used for precise fault injection also stay direct.

Add a facade export only when actual consumers share a coherent conceptual
vocabulary and the defining file is incidental to those consumers. Keep explicit
imports and `__all__`, import definitions directly rather than chaining facades,
and exclude ownership machinery and local helpers. A clear domain module is
already a canonical location; do not collect all its names elsewhere. Check the
dependency graph before adding exports, including initialization of every parent
package. Root, helper, and protocol initialization must stay independent of
controller, configuration, OpenOCD planning, and Zephyr so the deployed helper
continues to run with only the standard library. A canonical internal import
does not enlarge the supported integration API.

Tests may directly exercise a deliberate internal abstraction through its normal
interface. Prefer normal construction and effect-boundary fakes over private
state mutation. Narrow private hooks may remain for deterministic acquisition,
publication, reader-settlement, or cleanup fault injection when the precise
interleaving is the regression under test; document that reason at the hook.
See [test design](docs/development/test_design.md).

Environment variables read by the product use the
`ZEPHYR_REMOTE_OPENOCD_` prefix. Reserve the shorter `ZRO_` prefix for the test
harness, validation tools, test payloads, and generated internal markers. In
particular, `ZEPHYR_REMOTE_OPENOCD_CONFIG` selects the product YAML file, while
`--hardware-inventory` selects the local YAML hardware inventory used by external
tests. `ZRO_RECORD` and `ZRO_RECORD_OPENOCD_VERSION` control the runner's test-only
[recording mode](docs/development/testing.md#runner-recording-mode). Do not
introduce a public product setting with the `ZRO_` prefix.

Install gitlint's default `commit-msg` hook with:

```sh
.venv/bin/gitlint install-hook
```

The installer refuses to replace an existing `commit-msg` hook. Commit messages
must pass gitlint's default rules; use an imperative subject, a blank line, and
a concise body that explains the change.

Zephyr integration tests require a Zephyr 4.4.x environment but no hardware
inventory. SSH integration and hardware tests use a contributor-provided local
hardware inventory. See [`docs/development/testing.md`](docs/development/testing.md)
before running them. See [`SRS`](docs/requirements/SRS.md) for behavior and
requirements, [`SAD`](docs/architecture/SAD.md) for architecture and rationale,
[`protocol.md`](docs/architecture/protocol.md) for the helper wire contract, and
[`test_design.md`](docs/development/test_design.md) for guidelines on writing tests,
and [`assurance_methods.md`](docs/development/assurance_methods.md) for bounded
exploratory campaigns beyond the maintained suite.
Never commit credentials, host/device details, generated builds, or `.scratch/`
files.

Submit focused commits. Describe behavior, validation performed, and fixture
coverage in pull requests. Update the SRS
or SAD when a lifecycle, protocol, compatibility, or other architectural
decision changes.
