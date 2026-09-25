# Test design and regression value

Tests are regression guards, not line-by-line mirrors of the implementation.
Prefer tests that survive behavior-preserving refactors and fail when an
observable contract or meaningful invariant changes.

- Prefer real production objects for deterministic, inexpensive in-process
  collaborators. A small graph of real objects usually provides more confidence
  than mocks that reproduce their collaboration.
- Mock or fake effectful boundaries such as subprocesses, SSH, sockets,
  filesystem effects, clocks, selectors, and external commands. Avoid replacing
  ordinary production collaborators merely because they are dependencies.
- When a mock is appropriate, use `autospec`, `create_autospec`, or `spec_set`
  so signature or attribute drift fails at the test boundary. Avoid
  unconstrained `Mock` or `MagicMock` objects that silently accept invalid use.
- Prefer a small semantic fake over elaborate mock choreography when a boundary
  has meaningful state or several related operations. A fake should provide
  controllability, not duplicate the production algorithm, parser, lifecycle,
  or state machine.
- When a production abstraction is intentionally designed for specialization,
  a small test subclass may override one narrow effectful operation while
  retaining the surrounding production behavior. Do not subclass the system
  under test to expose private state or bypass its public interface.
- Assert observable behavior, state transitions, ownership and cleanup effects,
  protocol events, returned values, and required boundary interactions. Avoid
  assertions whose only purpose is to prove that one private helper called
  another in a particular order.
- Do not assert private wording, logging text, diagnostics, serialized details,
  or exact call shapes unless that form is a documented interface or
  compatibility contract.
- Keep time and concurrency deterministic. Advance fake clocks at meaningful
  events rather than coupling them to a fixed number of clock reads, and use
  events, barriers, pipes, socket readiness, controlled selectors, or equivalent
  synchronization instead of `sleep()` or short real-time deadlines.
- Within the selected test layer, prefer the highest inexpensive behavioral path
  that exercises the public production flow with real in-process collaborators.
  Do not split one stable behavioral check into several private-helper tests
  merely to increase coverage or fault localization.
- Do not test Python, pytest, or standard-library behavior that the project does
  not own unless the project wraps it with a contract worth protecting.
- Remove redundant or low-value tests rather than preserving them only for
  coverage. When replacing a brittle test, preserve the behavioral requirement
  it protected rather than its implementation-specific structure.
- Treat excessive test difficulty as a design signal. Deep patching, large mock
  graphs, private-state mutation, exact call choreography, or timing tricks can
  indicate unclear ownership or a missing production seam. Consider improving
  the production design before adding more test machinery.
