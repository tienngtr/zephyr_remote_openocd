# Test design guidelines

When adding or modifying tests, optimize for regression value, determinism, type
safety, and resistance to harmless refactoring. Tests should model stable
contracts and meaningful state transitions rather than mirror implementation
details.

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

* Keep time and concurrency deterministic. Do not rely on `sleep()`, short
real-time deadlines, call-count-driven fake clocks, retries, or scheduling
assumptions. Advance fake clocks at meaningful events and use events, barriers,
pipes, socket readiness, controlled selectors, or equivalent synchronization.

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
