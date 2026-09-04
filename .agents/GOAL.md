# Repository Comprehension and Contributor UX Goal

## Objective

Make Zephyr Remote OpenOCD easier to understand, use, test, and contribute to
without changing its current product behavior. Separate information by audience,
make external-test requirements reproducible and visible, and provide dependable
automated and release-validation entry points.

## Required Outcomes

- Users have a short, accurate path from obtaining a Git-free copy or checkout to
  setup, activation, configuration, and every supported west operation.
- Contributors can find current architecture, requirements, compatibility
  contracts, testing prerequisites, and contribution checks without reading
  implementation history.
- Requirements remain normative, architecture describes the present design,
  traceability maps acceptance criteria to evidence, and validation records
  results separately.
- Protocol 1 and the V1 configuration schema each have one clearly identified
  canonical contract consistent with implementation and tests.
- Maintained tests have explicit dependency layers. The default test command is
  self-contained; Zephyr, SSH, hardware, benchmark, and strict-release runs are
  selected explicitly.
- The default local test command completes successfully from a clean checkout,
  including all unit and local-integration coverage that does not need external
  infrastructure.
- The maintained test suite is pytest-native and uses its fixture,
  parametrization, selection, teardown, and diagnostic facilities rather than a
  mechanical compatibility wrapper.
- A tracked, board-neutral template fully describes the ignored external test
  inventory, including build recipes, probes, forwarded environment, serial
  framing, operation profiles, capabilities, and expected observations.
- Hardware capabilities are independent: an unsupported optional capability
  does not suppress otherwise supported operations.
- Hardware-free GitHub CI and an explicit strict native-Linux release-validation
  workflow provide repeatable outcomes.
- The GitHub Actions checks workflow completes successfully from a clean checkout
  using only its declared developer dependencies.
- Product version `0.1.0` is available without Git metadata and is described as
  initial-development SemVer, distinct from Protocol 1 and the V1 config schema.

## Scope

- Root navigation and contributor/agent guidance.
- User, requirements, architecture, protocol, development, traceability, and
  validation documentation.
- Test organization, framework, shared test infrastructure, external fixture
  configuration, and capability selection.
- Developer-only dependency declaration, hardware-free CI, release orchestration,
  documentation checks, and repository hygiene checks.
- Migration of existing ignored local fixtures for validation without committing
  their concrete values.

## Non-Goals

- New runner, helper, OpenOCD, GDB, RTT, semihosting, or serial functionality.
- Changes to product lifecycle, command semantics, transport architecture,
  configuration behavior, or protocol behavior.
- Pip packaging or installation of the product, an installer, or automatic user
  environment/repository modification.
- A generated documentation site, hardware reservation service, or parallel
  hardware execution.
- Committing host aliases, credentials, actual lab target identities, probe
  serials, device paths, or other developer-local fixture values.
- Retaining completed prototype diaries solely as historical records.
- Claiming WSL2 validation before PG-012 and PG-013 run on a real WSL2 system.

## Constraints

- Preserve the self-contained Zephyr module and arbitrary-path, Git-free
  distribution model.
- Preserve Zephyr 4.4 and Python 3.12+ support and the accepted Zephyr-provided
  `pyelftools` runtime dependency.
- Preserve all stable `REQ-*` and `AC-*` identifiers and their normative meaning.
- Preserve the frozen V1 TOML schema, strict unknown-key handling, and canonical
  `resources/config.toml.example`.
- Preserve frozen Protocol 1 semantics; incompatible or helper-dependent changes
  require Protocol 2.
- Keep production code and general product documentation board/vendor-agnostic.
- Keep recording mode free of SSH, helper, OpenOCD, GDB, and hardware I/O.
- Keep third-party test tooling developer-only; no new product installation step
  or unnecessary production dependency may result.
- Normal external tests may skip unavailable infrastructure with a precise reason;
  strict release validation must fail when required native-Linux layers or
  capabilities are absent.

## Acceptance Criteria

- A clean-install test executes the documented setup and recording flow from an
  arbitrary Git-free copy under an isolated home, including config creation,
  permissions, preservation on rerun, and module discovery from the copy alone.
- All repository-relative documentation links resolve; removed paths and
  prototype documents have no remaining references.
- Every prior `REQ-*` and `AC-*` ID remains unique in the SRS, and every `AC-*`
  has a separately maintained traceability entry.
- The SRS has no dependency on SAD section numbers or test implementation names;
  user documentation has no PG or test-method references.
- Fixed Protocol 1 compatibility fixtures and V1 configuration behavior retain
  their existing semantics.
- With all external configuration cleared, the default test command collects and
  passes only self-contained unit and local-integration tests.
- A clean-checkout run of the default local test command passes; any socket
  permissions needed by local-integration tests are provided by the test
  environment rather than accepted as silent coverage loss.
- Every external test belongs to one documented dependency layer and reports
  missing prerequisites according to the normal or strict policy.
- The tracked inventory example validates without local edits and contains no
  concrete lab identity; invalid fields and references produce precise errors.
- Synthetic inventory tests prove RTT/thread-info incapability does not suppress
  flash, debug, attach, or debugserver collection.
- Declared firmware recipes create required build directories during the test
  session rather than relying on cached developer builds.
- Serial settings and environment forwarding are consumed from structured test
  data and covered by non-hardware tests plus the applicable external validation.
- CI passes from a clean checkout using declared developer dependencies and no
  SSH or hardware.
- The corresponding GitHub Actions workflow has a successful completed run for
  the current implementation, including tests, formatting, lint, static
  compatibility, and whitespace checks.
- Strict release validation reports each required capability, performance result,
  cleanup result, and PG-012/PG-013 deferral, and returns nonzero for missing
  mandatory evidence or leaks.
- No actual lab fixture, generated build, cache, scratch artifact, credential, or
  developer-local absolute path is tracked.
- Production files remain behaviorally unchanged and all prior behavioral
  coverage remains passing.

## Required Verification

- Automated checks for Markdown links, requirement/acceptance identifiers,
  traceability completeness, canonical config consistency, Protocol 1 fixtures,
  inventory validation, test-layer selection, and release-driver failure policy.
- Default pytest suite with external configuration cleared.
- Explicit Zephyr integration and Git-free clean-install acceptance.
- Explicit SSH and capability-gated hardware validation using the ignored local
  inventory when the environment is available.
- Startup-overhead release benchmark using its conservative baseline
  interpretation.
- Ruff check and format check, Pylint, Vermin, and `git diff --check`.
- Final local and remote process, forward, and workspace leak checks.
- Review of the production-tree diff to confirm no behavior change.

## Unresolved Decisions

None at contract creation. New substantive product, compatibility, or scope
questions must be raised with the user and recorded here only if they change the
project contract. Implementation findings and ordinary tactical choices belong
in `.agents/PLAN.md`.
