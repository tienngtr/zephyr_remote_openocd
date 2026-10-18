# Test design guidelines

When adding or modifying tests, optimize for regression value, determinism, type
safety, and resistance to harmless refactoring. Tests should model stable
contracts and meaningful state transitions rather than mirror implementation
details.

Production authority, ownership, effect-entry, settlement, and
failure-composition patterns are defined in
[SAD §43.1](../architecture/SAD.md#431-architecture-maintenance-invariants)
and [§43.2](../architecture/SAD.md#432-extension-recipes). Lifecycle and
concurrency tests should protect those architectural invariants rather than
invent parallel test-only models.

* Prefer real production objects over mocks or synthetic objects. If a
collaborator is deterministic, inexpensive, and does not cross an external
boundary, construct and use the real production object. A small graph of real
objects exercising their normal collaboration is usually more valuable than
reproducing that collaboration with mocks or hand-built state.

* Construct the system under test through its normal production initialization
when practical. Avoid `object.__new__()`, manual partial initialization, or
similar techniques that bypass constructor-established invariants merely to
simplify test setup. Use them only when construction itself is the behavior
under test or normal construction necessarily crosses an unsuitable external
boundary.

* Mock or fake effectful boundaries, not ordinary production collaboration.
Appropriate boundaries include subprocesses, SSH, sockets, filesystem effects,
clocks, selectors, and external commands. Avoid replacing internal production
objects merely because they are dependencies.

* Keep test code meaningfully type-checked. Tests are part of mypy validation
and should benefit from it. Do not routinely use `Any`, `cast(Any, ...)`, broad
`# type: ignore`, or partially initialized objects to make test setup
type-check. When a typing escape is genuinely required at a dynamic or untyped
boundary, keep it narrow and local so type checking remains effective elsewhere.

* Prefer typed test doubles that conform to the production abstraction. When a
mock is appropriate, use `autospec`, `create_autospec`, or `spec_set`. When a
dependency has meaningful state or several related operations, prefer a small
semantic fake whose interface remains statically checked. Do not hide an
incompatible fake behind `cast(Any, ...)`.

* Use a small test subclass when the production abstraction is intentionally
designed for specialization. Override only the narrow operation that needs test
control, retain the real surrounding production behavior, and use `@override`
so mypy verifies that the test double still matches the production signature.
Do not subclass the system under test merely to expose private state or bypass
its public interface.

* Make forbidden operations fail explicitly in narrow test subclasses. If a test
double intentionally supports only one production operation, override other
effectful operations with an immediate `AssertionError`, for example
`"run_stream() is not expected in this test"`. This documents the intended test
seam and ensures that production code cannot silently cross a different boundary
while the test continues to pass.

* Keep substantive child programs in checked source files. A subprocess
scenario, fake SSH or helper peer, fault-injection wrapper, or remote program
with branches, loops, functions, monkeypatching, or signal, PTY, or
process-lifecycle behavior belongs in a `tests/fixtures/` module with a
`main()`. Pass its parameters through argv, environment, files, or
descriptors. A program sent as remote `python3 -c` text stays a standalone
standard-library module whose text the test reads. Patch with
`unittest.mock.patch.object` so a renamed target fails rather than silently
adding an attribute. Straight-line choreography of a few statements, such as
writing a frame and waiting on a gate, may remain an inline `python -c`
string.

* Use `Protocol` for genuine narrow production roles, not as test-only typing
scaffolding. If production code conceptually depends on a small structural
interface, model that role with a `Protocol` and let typed fakes satisfy it
naturally. Do not introduce a protocol solely to make an otherwise incompatible
test fake type-check, and do not duplicate an entire concrete class API merely
for tests. A protocol should express a real production dependency or ownership
boundary.

* Do not recreate production behavior in test doubles. Fakes provide
controllability; they should not contain a second implementation of the
production algorithm, parser, lifecycle, validation logic, or state machine.
If that becomes necessary, use the real object or improve the production seam.

* Test observable behavior and invariants, not private choreography. Assert
externally meaningful state, returned values, protocol events, ownership and
cleanup effects, and required boundary interactions. Avoid assertions whose
only purpose is to prove that one private helper called another. Exact ordering
should be asserted only when ordering itself is a required lifecycle or
ownership invariant.

* Do not assert private wording unless the text is contractual. Internal
exception messages, diagnostics, logging strings, and helper wording should not
become accidental APIs. Prefer exception types or semantic results unless exact
text is externally required.

* Make direct internal-abstraction tests explicit. Import the normal names
from a cohesive `_module` when testing a package-internal role, such as output
decoding, process disposal, or forwarding readiness. Use the existing ordinary
entry point when it already covers the behavior (for example `decode_command`,
not its private START parser). Do not promote private state to a public property
solely to satisfy tests. Acquire session collaborators through normal
construction rather than replacing private fields after initialization.

* A private hook is an exception for a concrete regression, not an alternate
API. Reader joins and dispatch gates may synchronize recognized versus dispatched
facts; constructor-return hooks may expose an otherwise inaccessible ownership
handoff; cleanup fault injection may force a physically unconfirmed outcome.
Keep the real surrounding production path, explain the boundary at the hook,
and assert observable outcomes and cleanup effects. Do not spread these hooks
into ordinary behavior tests or hide private access behind generic test helpers.

* Keep time and concurrency deterministic. Do not rely on `sleep()`, short
real-time deadlines, call-count-driven fake clocks, retries, or scheduling
assumptions. Advance fake clocks at meaningful events and use events, barriers,
pipes, socket readiness, controlled selectors, or equivalent synchronization.

* Derive lifecycle and concurrency regressions from an explicit interleaving.
Distinguish an effect succeeding from ownership adoption, and distinguish a
fact occurring, being recognized, blocking during publication, entering a
queue, and being dispatched. Synchronize the test at the boundary relevant to
the contract instead of assuming those stages happen atomically.

* For resource-ownership regressions, interrupt immediately before or after the
meaningful handoff and assert the externally relevant cleanup result. For
ordering or precedence regressions, arrange the competing facts
deterministically and assert which outcome becomes primary and which
diagnostics remain visible. A race test should be explainable as a concrete
event sequence before it is encoded as test machinery.

Tests that deliberately deliver native SIGINT through a raising Python handler
use `@pytest.mark.usefixtures("defer_cyclic_gc")`. The shared fixture defers
cyclic GC and restores its prior state so an unrelated GC weakref callback
cannot swallow the expected interrupt as an unraisable exception. Keep the real
signal, ownership, cleanup, mask, and failure-precedence assertions. Tests that
inject a Python exception or use non-raising signal observers do not need it.
Runner cancellation tests deliver real SIGINT under the operation's non-raising
capture and synchronize through pipes, FIFOs, PTYs, or a hook immediately
before the wait sleeps; they need no GC deferral. A test proving that a wait is
woken by native delivery blocks SIGINT on the waiting thread, so only the
wakeup descriptor can end the wait.

* Keep each test focused on one contractual behavior. Setup may involve several
real production objects, but assertions should have a clear behavioral purpose
rather than forming a broad snapshot of incidental implementation state.

* Do not test Python, pytest, or standard-library behavior that the project
does not own. Protect project-specific logic and important integration
assumptions, not the documented semantics of `dataclass`, `Event`, `subprocess`,
`socket`, and similar facilities.

* Remove redundant or low-value tests rather than preserving them for coverage.
When behavior is already protected at the same or a more meaningful boundary,
deleting the duplicate is preferable to retaining another implementation-coupled
test. When replacing a brittle test, preserve the behavioral requirement it
protected rather than its structure.

* Prefer behavioral coverage at the highest inexpensive level. If behavior can
be exercised deterministically through the public production flow with real
in-process collaborators, prefer that over several narrow private-helper tests.
Use focused unit tests when they provide useful fault localization or when the
real boundary is expensive or nondeterministic.

* Treat testing and typing friction as design signals. Repeated
`cast(Any, ...)`, `object.__new__()`, private-state mutation, deep patching,
large mock graphs, oversized test subclasses, exact call choreography, or
elaborate timing machinery can indicate that the production code lacks a clear
ownership boundary or controllable seam. Investigate the design before adding
more test machinery.

A useful rule of thumb is: real object first; narrow typed subclass for an
intentional abstraction; `Protocol` for a genuine narrow production role;
constrained mock or semantic fake at an effectful boundary; `Any` only as a
last, localized escape.
