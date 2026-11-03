# Test design guidelines

When adding or modifying tests, optimize for regression value, determinism, type
safety, and resistance to harmless refactoring. Tests should model stable
contracts and meaningful state transitions rather than mirror implementation
details.

These guidelines apply to every test in the maintained suite. For the test
layers and how to run them, see [`testing.md`](testing.md); for exploratory
campaigns beyond the maintained suite, see
[`assurance_methods.md`](assurance_methods.md).

## Deciding what to test

* Add or strengthen regression coverage only when it materially improves
confidence in affected behavior or a contract. Do not add a test mechanically
for each changed line or helper.

* Within the smallest test layer that covers the change, test through the most
public entry point that is still inexpensive and deterministic. If behavior can
be exercised deterministically through the public production flow with real
in-process collaborators, prefer that over several narrow private-helper tests.
Use focused unit tests when they provide useful fault localization or when the
real boundary is expensive or nondeterministic.

* When practical, verify that new or materially changed regression coverage
detects the pre-fix behavior. A passing test is not useful evidence if its
mock, fake, fixture, or setup bypasses the production path that contained the
defect. Trace that boundary and repair coverage that passes for the wrong
reason rather than adding more tests around the same faulty setup.

* Remove redundant or low-value tests rather than preserving them for coverage.
When behavior is already protected at the same or a more meaningful boundary,
deleting the duplicate is preferable to retaining another implementation-coupled
test. When replacing a brittle test, preserve the behavioral requirement it
protected rather than its structure.

* Do not test Python, pytest, or standard-library behavior that the project
does not own. Protect project-specific logic and important integration
assumptions, not the documented semantics of `dataclass`, `Event`, `subprocess`,
`socket`, and similar facilities.

* Do not unit-test repository tooling merely because it contains code. Static
check wrappers, lint configuration, test helpers, and orchestration normally
prove themselves by being run. Test them only when non-trivial logic could
silently invalidate validation, safety, cleanup, or external-test results.

* Treat testing and typing friction as design signals. Repeated
`cast(Any, ...)`, `object.__new__()`, private-state mutation, deep patching,
large mock graphs, oversized test subclasses, exact call choreography, or
elaborate timing machinery can indicate that the production code lacks a clear
ownership boundary or controllable seam. Investigate the design before adding
more test machinery.

## Choosing a test double

These options are ordered from least to most artificial. The rules below decide
which one fits a dependency; when more than one fits, prefer the earlier one.

1. The real production object.
2. A narrow typed subclass of an effect boundary or of an abstraction designed
   for specialization.
3. A typed fake satisfying a `Protocol` for a genuine narrow production role.
4. A constrained mock or semantic fake at an effectful boundary.
5. `Any`, only as a last, localized escape.

* Prefer real production objects over mocks or synthetic objects. If a
collaborator is deterministic, inexpensive, and does not cross an external
boundary, construct and use the real production object. A small graph of real
objects exercising their normal collaboration is usually more valuable than
reproducing that collaboration with mocks or hand-built state.

* Construct the system under test through its normal production initialization
when practical. Acquire its collaborators through normal construction rather
than replacing private fields after initialization, except through a documented
[private hook](#internal-abstractions-and-private-hooks). Avoid
`object.__new__()`, manual partial initialization, or similar techniques that
bypass constructor-established invariants merely to simplify test setup. Use
them only when construction itself is the behavior under test or normal
construction necessarily crosses an unsuitable external boundary.

* Use a small test subclass for an effect boundary, such as `SshCommand`, or
for a production abstraction intentionally designed for specialization.
Override only the narrow operation that needs test control and retain the real
surrounding production behavior. mypy checks every override against the
production signature; also mark overrides with `@override`, so that renaming or
removing the production method makes mypy report the stale override. Do not
subclass the system under test merely to expose private state or bypass its
public interface.

* Make forbidden operations fail explicitly in narrow test subclasses. If a test
double intentionally supports only one production operation, override other
effectful operations with an immediate `AssertionError`, for example
`"run_stream() is not expected in this test"`. This documents the intended test
seam and ensures that production code cannot silently cross a different boundary
while the test continues to pass.

* Use `Protocol` for genuine narrow production roles, not as test-only typing
scaffolding. If production code conceptually depends on a small structural
interface, model that role with a `Protocol` and let typed fakes satisfy it
naturally. Do not introduce a protocol solely to make an otherwise incompatible
test fake type-check, and do not duplicate an entire concrete class API merely
for tests. A protocol should express a real production dependency or ownership
boundary.

* Mock or fake effectful boundaries, not ordinary production collaboration.
Appropriate boundaries include subprocesses, SSH, sockets, filesystem effects,
clocks, selectors, and external commands. Avoid replacing internal production
objects merely because they are dependencies.

* Prefer typed test doubles that conform to the production abstraction. When a
mock is appropriate, constrain it: `autospec` and `create_autospec` reject calls
that no longer match the production signature, and `spec_set` also rejects
unknown attributes. When a dependency has meaningful state or several related
operations, prefer a small semantic fake whose interface remains statically
checked. Do not hide an incompatible fake behind `cast(Any, ...)`.

* Do not recreate production behavior in test doubles. Fakes provide
controllability; they should not contain a second implementation of the
production algorithm, parser, lifecycle, validation logic, or state machine.
If that becomes necessary, use the real object or improve the production seam.

## Typing in tests

mypy checks tests with the same complete-annotation rules as production code:
annotate every function, including tests, fixtures, harnesses, fakes, and
fixture programs.

* Annotate pytest fixtures with their real types, such as `Path`,
`pytest.MonkeyPatch`, or `pytest.CaptureFixture[str]`, and parametrized
arguments with the type of their values.

* Do not routinely use `Any`, `cast(Any, ...)`, broad `# type: ignore`, or
partially initialized objects to make test setup type-check. When a typing
escape is genuinely required at a dynamic or untyped boundary, keep it narrow
and local so type checking remains effective elsewhere.

* A test that deliberately passes an ill-typed value to exercise runtime
validation keeps the precise parameter type and marks only that call with a
narrow `# type: ignore[arg-type]`.

## Child programs

Put substantive child programs in `tests/fixtures/` modules with a `main()`, so
that the static checks cover them like other test code. A subprocess scenario,
fake SSH or helper peer, fault-injection wrapper, or remote program is
substantive when it has branches, loops, functions, monkeypatching, or
non-trivial signal, PTY, or process-lifecycle behavior. Straight-line
choreography of a few statements, such as writing a frame and waiting on a
gate, or setting a signal disposition and pausing, may remain an inline
`python -c` string.

* Pass a fixture program's parameters through argv, environment, files, or
descriptors rather than formatting them into the program text, so that the
module stays static and checked.

* Write a program sent as remote `python3 -c` text as a standalone module that
imports only the standard library, because the remote side runs only that text;
the test reads the module's text and sends it.

* In fixture programs, patch with `unittest.mock.patch.object` rather than
assigning attributes directly, so that a renamed target fails instead of
silently gaining a new attribute.

## Assertions

* Test observable behavior and invariants, not private choreography. Assert
externally meaningful state, returned values, protocol events, ownership and
cleanup effects, and required boundary interactions. Avoid assertions whose
only purpose is to prove that one private helper called another. Exact ordering
should be asserted only when ordering itself is a required lifecycle or
ownership invariant.

* Assert exact prose, serialized bytes, command formatting, internal call
order, tool flags, or private structure only when that exact form is a required
interface or compatibility contract. For internal exception messages,
diagnostics, logging strings, and helper wording, assert exception types or
semantic results instead, so that they do not become accidental APIs.

* Do not make assertions broader or more exact merely to increase coverage. A
behavior-preserving refactor should normally not require test changes.

* Keep each test focused on one contractual behavior. Setup may involve several
real production objects, but assertions should have a clear behavioral purpose
rather than forming a broad snapshot of incidental implementation state.

## Internal abstractions and private hooks

* Set up ordinary sessions through `controller.api`. To test a
package-internal role, such as output decoding, process disposal, or
forwarding readiness, import it from its defining module and use its normal
interface; the import rules are in
[`CONTRIBUTING.md`](../../CONTRIBUTING.md#internal-apis-in-tests). Use the
existing ordinary entry point when it already covers the behavior (for example
`decode_command`, not its private START parser). Do not promote private state
to a public property solely to satisfy tests.

* A private hook is an exception for a concrete regression, not an alternate
API. Examples:
  * reader joins and dispatch gates that separate recognized from dispatched
    facts;
  * a blocked event queue that holds a fact during publication;
  * constructor-return hooks that expose an otherwise inaccessible ownership
    handoff;
  * cleanup fault injection that forces a physically unconfirmed outcome.

  Keep the real surrounding production path, explain the boundary at the hook
  so a later reader can tell why the private access is needed, and assert
  observable outcomes and cleanup effects. Do not spread these hooks into
  ordinary behavior tests or hide private access behind generic test helpers.

## Time, concurrency, and signals

* Keep time and concurrency deterministic. Do not use `sleep()`, elapsed-time
assertions, short real-time deadlines, shortened production timeouts,
call-count-driven fake clocks, retries, or scheduling assumptions to coordinate
behavior. Use observable handshakes: events, barriers, pipes, socket
readiness, process exit, controlled selectors or fakes, or fake clocks advanced
at meaningful events rather than by how often production code reads them. A
test of a production deadline may let that deadline expire; it must not shorten
unrelated production timeouts or add configuration to do so.

* Treat test timeouts as generous deadlock safety nets only; a successful test
must not depend on their expiry. A real-process test may poll an explicit
condition with a generous overall deadline, but must not assert normal
completion timing.

* Do not add production configuration solely to make a test convenient; adapt
the test to the production interface unless configurability is independently
needed.

* Lifecycle and concurrency tests protect the architectural invariants in
[SAD §43.1](../architecture/SAD.md#431-architecture-maintenance-invariants)
and follow the patterns in
[§43.2](../architecture/SAD.md#432-extension-recipes) for authority,
ownership, effect entry, settlement, and failure composition; do not invent a
parallel test-only model of them.

* Derive lifecycle and concurrency regressions from an explicit interleaving:
state the concrete event sequence before encoding it as test machinery.
Distinguish an effect succeeding from ownership adoption, and distinguish a
fact occurring, being recognized, blocking during publication, entering a
queue, and being dispatched. Synchronize the test at the boundary relevant to
the contract instead of assuming those stages happen atomically.

* For resource-ownership regressions, interrupt immediately before or after the
meaningful handoff and assert the externally relevant cleanup result.

* For ordering or precedence regressions, arrange the competing facts
deterministically and assert which outcome becomes primary and which
diagnostics remain visible.

### Signal delivery tests

Whether a test needs `@pytest.mark.usefixtures("defer_cyclic_gc")` depends on
how it delivers the interrupt:

| Test kind | `defer_cyclic_gc` |
| --- | --- |
| Native SIGINT through a raising Python handler | Required |
| Injected Python exception, or non-raising signal observer | Not needed |
| Runner cancellation: real SIGINT under the operation's non-raising capture | Not needed |

The shared fixture defers cyclic GC and restores its prior state so an
unrelated GC weakref callback cannot swallow the expected interrupt as an
unraisable exception. It changes only GC timing, so keep the real signal,
ownership, cleanup, mask, and failure-precedence assertions.

Runner cancellation tests synchronize through pipes, FIFOs, PTYs, or a hook
immediately before the wait sleeps. To prove that native delivery wakes a
wait, block SIGINT on the waiting thread, so that only the wakeup descriptor can
end the wait.
