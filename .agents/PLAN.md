# Repository Comprehension and Contributor UX Plan

## Status Legend

- `[not started]`: no implementation work has begun.
- `[in progress]`: work is active but its exit criteria are not satisfied.
- `[blocked]`: progress requires a named decision or external prerequisite.
- `[done]`: exit criteria and milestone-specific checks have passed.

## Current State

- `[done]` Repository and documentation audit completed for planning.
- `[done]` Goal, constraints, material ambiguities, risks, and measurable evidence
  agreed with the user and captured in `.agents/GOAL.md`.
- `[in progress]` Product/document/test refactor implementation; M1 is complete.

The production module is already organized into board-independent remote logic,
a Zephyr 4.4 adapter, and a west runner entry point. Its config schema and helper
Protocol 1 are frozen. The main problems are information architecture and test
infrastructure: the large SRS and SAD mix normative/current material with
traceability and historical results; users have only the root README; contributor
and test prerequisites are dispersed; external test layers are based on broad
`unittest` discovery and four ignored JSON fixture interfaces; some nominal unit
tests exercise local processes and sockets; optional hardware capabilities are
not consistently independent.

## Target Architecture

### Documentation

Use root entry points `README.md`, `CONTRIBUTING.md`, and `AGENTS.md`, backed by
plain Markdown under audience directories:

```text
docs/user
docs/requirements
docs/architecture
docs/development
docs/traceability
docs/validation
```

The SRS is normative and does not depend on lower-level project documents or
test implementation. The SAD references requirements and describes current
design. Protocol 1 has a canonical exact contract. Traceability and release
evidence are separate. The shipped TOML template remains the canonical
product-config example.

### Tests

Use pytest-native tests split into `unit`, `local_integration`,
`zephyr_integration`, `ssh_integration`, `hardware`, and `release`. Plain
`pytest` selects the first two only. Registered markers and explicit paths select
external layers. Shared fixtures own resource setup/cleanup and expose each
hardware profile/capability as a distinct test node.

### External Validation Configuration

Use one ignored TOML inventory selected by `--hardware-config`, with
`ZRO_HARDWARE_CONFIG` as fallback and CLI precedence. A tracked board-neutral
example documents schema version 1. Host entries contain SSH/OpenOCD/product
config inputs. Target entries contain tools and named build recipes, serial
endpoints, and profiles. Profiles refer to builds/endpoints and declare probe,
environment, command data, expectations, and independent capabilities.

Tests generate temporary frozen-schema product configs from host entries.
Profile environment keys must be in the host allow-list. Relative application
paths resolve under the configured Zephyr tree; absolute external applications
remain valid. Profiles inherit host/target defaults only, never other profiles.

### Automation

GitHub Actions runs hardware-free checks on Python 3.12 with Zephyr v4.4.0 and
Zephyr SDK 1.0.1. A Python release driver runs the full native-Linux validation
serially, enforces capability coverage and cleanup, runs the benchmark/static
checks, and treats only PG-012/PG-013 as deferred without WSL2.

## Important Design Decisions

- `[done]` Use SemVer `0.1.0` in a Git-independent root `VERSION` file.
- `[done]` Treat product SemVer, Protocol 1, and V1 config schema as separate
  version domains.
- `[done]` Keep documentation as plain Markdown with README plus focused guides.
- `[done]` Remove completed prototype narrative rather than archive it.
- `[done]` Preserve all `REQ-*` and `AC-*` identifiers; relocate evidence.
- `[done]` Migrate fully to pytest-native tests, not permanent unittest wrappers.
- `[done]` Declare developer tooling in `requirements-dev.txt`; product remains
  unpackaged and self-contained.
- `[done]` Make default pytest execution self-contained.
- `[done]` Replace legacy JSON fixture variables cleanly with one TOML inventory.
- `[done]` Build hardware firmware from declared recipes once per test session.
- `[done]` Generate temporary product configs instead of referencing parallel
  hand-maintained config files.
- `[done]` Keep capabilities independent and enforce cross-fixture coverage only
  in strict release mode.
- `[done]` Add hardware-free GitHub CI and a Python strict-release driver.
- `[done]` Keep actual fixtures ignored and agent scratch notes under
  `.scratch/agents/`.

## Ordered Milestones

### M0 — Durable Work Contract `[done]`

- `[done]` Create the stable goal and status-tracked plan.
- `[done]` Conservatively update `AGENTS.md` without describing unimplemented structure as
  current fact.
- `[done]` Audit these three files for scope and terminology agreement.

Exit: the three files agree on behavior preservation, pytest target state,
documentation separation, fixture privacy, and verification obligations.

### M1 — Version and Documentation Skeleton `[done]`

- `[done]` Add `VERSION` containing `0.1.0`.
- `[done]` Create the `docs/` audience directories and index.
- `[done]` Move current SRS/SAD and performance evidence with
  link-preserving edits.
- `[done]` Add automated repository-relative Markdown-link validation.

Dependencies: M0.

Exit: all moved paths resolve, root navigation identifies each audience, and no
runtime path depends on Git metadata or a fixed checkout location.

### M2 — Separate Normative, Architectural, and Evidence Content `[done]`

- `[done]` Reduce SRS to requirements, assumptions, risks, non-goals, and
  AC definitions.
- `[done]` Rewrite SAD historical/completed-slice material into
  present-tense architecture only; correct section ordering while preserving
  meaning.
- `[done]` Extract the exact frozen Protocol 1 contract.
- `[done]` Create the V1 traceability matrix and validation record.
- `[done]` Remove completed PG history and retain only PG-012/PG-013
  deferral.
- `[done]` Add ID uniqueness and traceability-completeness checks.

Dependencies: M1.

Exit: requirement IDs and normative statements match the pre-refactor inventory;
SRS has no SAD/test dependency; protocol/config canonical sources are explicit.

### M3 — Pytest Foundation and Dependency Layers `[in progress]`

- `[done]` Add `requirements-dev.txt` and pytest configuration/marker
  registration.
- `[done]` Establish default collection of unit plus local integration
  only.
- `[not started]` Create shared test support for environment options and cleanup.
- `[done]` Move process/socket/helper cases out of unit tests.
- `[in progress]` Convert unit and local-integration coverage to pytest-native
  idioms.

Dependencies: M0.

Exit: external configuration cleared plus plain `pytest` passes without external
skips; maintained converted tests contain no `unittest.TestCase` subclasses.

### M4 — Unified TOML Inventory and Build Fixtures `[in progress]`

- `[done]` Define and document the test-only schema described above.
- `[done]` Add a complete neutral example and strict parser/reference
  diagnostics.
- `[done]` Implement named build recipes, structured serial settings,
  profiles, and independent capabilities.
- `[done]` Generate temporary product configs and validate forwarded
  environment keys.
- `[not started]` Extract the standard-library remote serial reader into reusable
  test support and make framing data-driven.
- `[not started]` Convert current ignored JSON fixtures locally for subsequent
  validation.

Dependencies: M3.

Exit: example and negative schema tests pass; synthetic profiles prove capability
independence; no actual lab value is tracked. Remaining serial-reader extraction
and local fixture conversion are prerequisites for external-test migration.

### M5 — External Pytest Migration `[not started]`

- `[not started]` Convert Zephyr, SSH, hardware, clean-install, and benchmark
  functional tests.
- `[not started]` Build each referenced recipe through a session-scoped fixture.
- `[not started]` Parameterize every profile/capability as a separately reported
  test.
- `[not started]` Remove the four legacy JSON environment interfaces.
- `[not started]` Implement normal skip diagnostics and strict
  missing-prerequisite behavior.

Dependencies: M3-M4.

Exit: each layer collects independently; debug-class collection no longer
requires thread-info capability; non-RTT targets retain all other applicable
coverage; old behavioral scenarios remain represented in traceability.

### M6 — User, Contributor, and Agent Guidance `[not started]`

- `[not started]` Refocus root README on install, setup, activation,
  configuration, and commands.
- `[not started]` Add configuration, operation, and troubleshooting guides.
- `[not started]` Add the human `CONTRIBUTING.md` workflow and clarify product
  versus developer dependencies.
- `[not started]` Update `AGENTS.md` to the implemented layout and test commands,
  and add concise playbooks for ordinary changes, hardware investigation, and
  release validation.
- `[not started]` Remove committed real lab identifiers from nominal
  examples/tests.

Dependencies: M1-M5.

Exit: every supported user operation and contributor check has one discoverable
and currently accurate entry point, with no PG/test names in user documentation.

### M7 — CI and Strict Release Driver `[not started]`

- `[not started]` Add GitHub Actions for lint/static, self-contained tests, Zephyr
  integration, clean install, and doc/interface checks.
- `[not started]` Add the Python strict-release driver with inventory validation,
  serial external layers, capability requirements, benchmark execution,
  cleanup/leak checks, and per-layer summary.
- `[not started]` Unit-test driver ordering, failure propagation, missing
  evidence, deferred WSL reporting, and exit status with injected command
  execution.

Dependencies: M2-M6.

Exit: CI passes from a clean checkout; simulated driver outcomes are deterministic
and missing native-Linux evidence is fatal while WSL-only gates remain deferred.

### M8 — Full Validation and Final Consistency Audit `[not started]`

- `[not started]` Run self-contained, Zephyr, clean-install, SSH, and configured
  hardware layers.
- `[not started]` Run the startup benchmark and all static/style checks.
- `[not started]` Verify normal, failure, and interruption cleanup and port reuse.
- `[not started]` Reconcile README, contributor guide, AGENTS, SRS, SAD,
  protocol, traceability, validation record, template, implemented selectors,
  and release summary.
- `[not started]` Scan tracked files and inspect the production-tree diff.

Dependencies: M1-M7.

Exit: every measurable acceptance criterion in `.agents/GOAL.md` has recorded
evidence; only PG-012/PG-013 are deferred; there is no unexplained product diff,
test regression, leaked resource, broken link, or tracked local artifact.

## Validation Matrix

- Documentation: link resolution, stale-path scan, ID uniqueness, AC coverage,
  layering rules, and board/local-identity scan.
- Interfaces: canonical V1 config parser tests and fixed Protocol 1 fixtures.
- Self-contained: plain pytest with external variables removed.
- External selection: collect-only and missing-prerequisite tests for every layer.
- Zephyr/distribution: Zephyr 4.4 integration and isolated Git-free clean install.
- SSH/hardware: inventory-selected capability nodes, environment forwarding,
  serial framing, normal/interruption cleanup, and final leak scan.
- Performance: manual release benchmark; median target statistic, diagnostic
  first/p95/worst statistics, conservative built-in baseline interpretation.
- Static/hygiene: Ruff, format, Pylint, Vermin, Git diff check, tracked-file scan,
  and production-tree diff review.

## Expected Semantic Commit Boundaries

1. Add version metadata and documentation navigation skeleton.
2. Separate requirements, architecture, protocol, traceability, and validation.
3. Establish pytest-native self-contained test architecture.
4. Add the unified TOML inventory and data-driven build/serial fixtures.
5. Convert Zephyr, SSH, hardware, and release tests to independent pytest nodes.
6. Add user, contributor, and durable agent guidance for the implemented flows.
7. Add hardware-free CI and strict release validation.
8. Record final evidence and resolve consistency/hygiene findings.

Each commit must leave links and its affected test layer passing. Do not mix a
product behavior change into these commits; stop and classify any discovered
behavioral defect separately.

## Risks and Open Questions

- `[not started]` Guard against changed normative meaning during SRS extraction by
  capturing and comparing the complete requirement/AC ID inventory and reviewing
  normative text diffs.
- `[not started]` Guard against hidden coverage loss by mapping old scenarios
  before deleting unittest classes and comparing collected pytest nodes.
- `[not started]` Keep inventory complexity bounded: explicit profiles and
  references only, no arbitrary inheritance or plugin mechanism.
- `[not started]` Classify hardware availability/contention independently from
  product and test failures; execute destructive profiles serially.
- `[not started]` Verify the pinned Zephyr/SDK CI setup is practical and cache it
  without weakening clean-checkout coverage.
- `[not started]` Keep developer pip usage visibly separate from product setup.
- `[not started]` Ensure documentation consistently distinguishes SemVer `0.1.0`,
  Protocol 1, and the V1 config schema.

No unresolved design decision is known at plan creation. If implementation finds
a substantive product, compatibility, or scope ambiguity, mark the affected
milestone `[blocked]`, record the question here, and ask the user rather than
choosing silently.
