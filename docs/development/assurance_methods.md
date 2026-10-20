# Exploratory assurance methods

This document describes bounded techniques for finding gaps that ordinary
example-based tests may not reveal: in the implementation, in the maintained
tests, or in the documented contracts themselves. It assumes familiarity with
conventional unit, integration, and hardware testing and explains what each
less common technique is for, what it can and cannot show, and what should be
kept afterwards.

[`test_design.md`](test_design.md) governs every test that enters the
maintained suite, including regressions and properties promoted from a
campaign. [`testing.md`](testing.md) describes the maintained test layers and
how to run them. This document does not repeat those rules.

The central distinction is between **maintained verification** and
**exploratory assurance**.

Maintained verification protects established behavior continuously. It must be
deterministic, understandable, and cheap enough to justify its ongoing
maintenance cost. Exploratory assurance is investigation: it may generate large
input spaces, perturb the implementation, repeat executions, inject unusual
failures, or build a temporary reference model to search for counterexamples.
Its tooling is disposable.

A campaign answers a bounded question, minimizes any counterexample it finds,
and retains only the knowledge that justifies permanent maintenance. Exploratory
tooling does not become maintained infrastructure merely because it found
something.

## Campaign discipline

### Scope

Start from a concrete uncertainty rather than from a technique. For example:
can arbitrary fragmentation of a framed byte stream change how it is
interpreted; can every acquired resource still be disposed when failure occurs
around an ownership handoff; does the maintained suite distinguish both sides
of an exact size limit? Choose the technique that suits the question, and name
the production surfaces in scope before running anything.

Define a stop condition at the same time. A campaign stops when the named
surfaces have been exercised substantially, every counterexample is explained,
and further work would mostly invent new targets rather than resolve the stated
uncertainty. Generated example counts, mutation scores, coverage percentages,
and stress iterations are evidence about effort, not objectives. Reaching a
large number is not a stop condition, and failing to reach one is not a reason
to continue.

### Oracles

An oracle decides whether an observed result is acceptable. State it before
generating cases. Useful oracles include:

- a documented invariant, such as "every frame a validated sender emits is
  accepted by the receiver" or "no process group outlives its session";
- a round-trip relation, such as encoding then decoding a value from the
  documented protocol domain;
- a small independent model of allowed states and transitions;
- an independently maintained reference implementation;
- externally observable resource state, such as processes, listening sockets,
  and remote workspace contents;
- a set of permitted outcomes where the contract deliberately allows more than
  one, such as which of two concurrent terminating causes becomes primary.

Write the oracle from the specification, not from production output or
production code. An oracle that reimplements the production algorithm tends to
agree with the same mistakes. Equally, an oracle that is stricter than the
contract produces false failures: a generic rule such as "the first injected
fault is always the primary failure" can be unsound when the contract permits a
racing consequence to be recorded first. Where the contract permits several
outcomes, the oracle accepts exactly that set; the
[differential acceptance decisions](../traceability/verification.md#differential-acceptance-decisions)
record which orderings are benign nondeterminism and which are required
behavior.

### Harness sensitivity

A run that could not have failed is not evidence. Before trusting a new
harness, show that it detects at least one representative defect of the kind
being sought: apply a temporary mutation to production code, feed the oracle a
deliberately wrong result, or reintroduce a known historical defect. For a
model, remove one safeguard from the modelled design and confirm that the
property it protects now fails.

Watch for oracles that pass vacuously. An absence check proves nothing if the
observer cannot see the thing that should be absent, for example a remote
cleanup check that runs after the operating system has already removed the
directory for unrelated reasons. A generator that never reaches the interesting
region, or a fault hook that never fires, looks the same as a clean result.
Record whether each injection point and generator strategy was actually
exercised.

### Execution and reproducibility

Keep campaign tooling and its third-party dependencies under the ignored
`.scratch/` tree (agent investigations under `.scratch/agents/`). Install
exploratory tools such as property-testing, mutation, or model-checking
packages into a scratch environment rather than adding them to
`requirements_dev.txt`. Run tools that rewrite source, such as mutation
testers, on a separate copy or worktree so an interrupted run cannot leave
mutated production code behind. Tools named in this document are examples to
help investigate a technique, not project requirements; choose whatever suits
the question.

Campaign tooling may be rougher than maintained tests, but failures must be
reproducible and interpretable. Record the code revision, seeds or settings,
and minimized counterexamples while investigating. Campaigns that use SSH hosts
or hardware follow the external-test rules in [`testing.md`](testing.md):
destructive work runs serially and cleanup is inspected before a target is
reused. Never record credentials, lab identities, or device paths in campaign
artifacts that may be shared.

Expect harness defects. Generators, oracles, and stand-ins written quickly will
be wrong in places; each failure must be classified as a production defect, a
harness defect, a contract ambiguity, or behavior of an unsupported environment
before anything changes in the repository.

### Minimization

When a campaign finds a problem, minimize it before changing production code.
The useful output is a short explanation of the violated contract and the
smallest counterexample that fails for that one reason. A large generated
input, a long random history, or a timing-dependent failure should not become
the regression when a small deterministic case protects the same behavior.

## Property-based testing and fuzzing

Property-based testing generates many inputs and checks a relation that should
hold for all of them. Fuzzing more broadly means exploring generated or mutated
input, from raw bytes to structured protocol messages. In practice the two
overlap: structured, property-checked generation is the common form here. In
Python, [Hypothesis](https://hypothesis.readthedocs.io/) is a widely used
property-based testing library: it draws inputs from composable strategies and
automatically shrinks a failing input toward a minimal counterexample.

Both are most useful where the input space is far larger than any reasonable
set of hand-written examples: parsers, serializers, framed protocols, path
normalization, numeric limits, and combinations of individually valid fields.

A property states behavior more general than one example. For a framed
protocol, useful properties include:

- for values in the documented protocol domain, encoding then decoding
  preserves the protocol value;
- splitting the byte stream at arbitrary positions does not change the decoded
  frame sequence;
- malformed input is rejected through the documented validation error, never
  through an unrelated exception type;
- complete and incremental decoding agree exactly at the size limit.

Scope each property to the contractual input domain, not to whatever a
low-level function happens to accept. A permissive encoder may accept values
outside the protocol, such as a mapping key that JSON serialization silently
converts to a string. That value does not round-trip, but the result violates
no contract unless a production sender can produce it.

For the second property, a generator can produce several valid frames,
concatenate them, split the stream at random positions, and feed the pieces to
the incremental decoder. This explores far more fragmentation patterns than
hand-picked examples, without making each split a maintained test.

Bias generation toward boundaries. Empty values, values one past a limit,
Booleans where integers are expected (Python's `bool` is an `int`), unusual or
invalid Unicode, duplicate and unknown keys, deep nesting, and cross-field
constraints are usually more productive than typical values. Raw random bytes
still suit the lowest framing layer, but structured generation reaches deeper
semantic checks far sooner.

Some generated cases fail because of the runtime rather than the code under
test: interpreter recursion limits, integer conversion limits, or an unusually
small thread stack. Classify these against the supported environment instead of
silently widening the support contract or adding defensive code for
configurations production never uses.

A clean run does not prove correctness. Its value is bounded by generator
reach, oracle strength, execution budget, and the surfaces exercised. Report
those limits rather than an example count.

## Mutation testing and deliberate defect injection

Mutation testing asks whether the maintained tests notice small, plausible
corruptions of the implementation. A tool systematically inverts conditions,
shifts boundary operators, removes validation, changes constants, or replaces
return values, runs the suite against each variant, and reports which variants
("mutants") no test detected. Deliberate defect injection is the same idea
applied by hand to a few chosen locations, usually to check that a new harness
or oracle is sensitive. In Python, [mutmut](https://github.com/boxed/mutmut) is
one such mutation testing tool; it generates mutants of the source, runs the
test suite against each, and reports the survivors.

Coverage shows that code ran; mutation shows whether any test would notice it
being wrong. A covered line can still hold an untested decision. For example,
temporarily changing a size check from `>` to `>=` shows whether any test
distinguishes an input exactly at the limit from one past it. Temporarily
accepting `bool` where a protocol integer is required shows whether type
validation is protected by an assertion or merely executed.

Mutation score is not an objective. Surviving mutants may be equivalent to the
original, unreachable through supported inputs, or masked by another check.
Triage survivors by behavioral meaning and add a maintained regression only when
a survivor exposes a distinct requirement or invariant. Masking deserves care:
if a component boundary has its own documented contract, such as one peer's
validation in a protocol, a defect masked by the other peer's check is still a
violation at that boundary and may still deserve a fix and a regression there.

Mutation verdicts are themselves evidence that can be wrong. A mutant counted
as killed because the suite timed out or ran out of resources under a heavily
parallel run has not been detected by an assertion. Recheck kills that relied
on timeouts, and avoid treating a crash of the harness as detection.

Broad mutation runs are expensive and noisy, so they belong to campaigns rather
than to every change. A few targeted mutants are the cheapest way to validate a
new property, model, or stress harness: if representative corruptions survive,
improve the oracle before trusting a large clean run.

## Fault injection at effect and ownership boundaries

Fault injection forces an operation to fail at a chosen point so cleanup,
ownership, failure precedence, and rollback can be observed. It suits
subprocesses, SSH, sockets, filesystem operations, writers, and resource
registration, where failure paths are rare in normal runs and hard to reach
otherwise.

Choose injection points from the ownership model in
[SAD §43.1](../architecture/SAD.md#431-architecture-maintenance-invariants),
not from whichever private calls are easy to patch. The important distinction
is often between an effect succeeding and its result being adopted by the next
owner. If a process is created but registering it fails immediately afterwards,
some owner must still be able to terminate and reap it. Injecting failures only
before creation and after normal adoption misses that interval.

A boundary campaign therefore injects failures, including `BaseException`
interruptions such as `KeyboardInterrupt`, immediately before and after each
acquisition and ownership transfer, and during cleanup itself. It checks
externally relevant results: no leaked process group, no listening forward, the
remote workspace removed or deliberately retained, exactly one terminal result,
and the established primary failure preserved alongside secondary diagnostics.

Exploratory fault injection may use temporary patches or narrow private hooks to
reach otherwise inaccessible points. Those hooks do not carry over
automatically. A defect found this way becomes a regression under the
constraints on private hooks and fault injection in
[`test_design.md`](test_design.md).

## Model-based and state-machine testing

Model-based testing compares production behavior with a smaller description of
the allowed states and transitions. State-machine testing generates sequences
of operations or events and checks that production accepts the valid sequences
and rejects or safely handles the rest.

It is most useful where individual events are simple but their order is not:
lifecycle protocols, acquisition and cleanup state, terminal events,
controller leases, and other temporal contracts.

For example, a small protocol model can track whether a session was created,
an attempt admitted, readiness published, or a terminal event sent. Generated
histories then exercise output before any attempt, readiness under a completion
policy that forbids it, duplicate terminal events, or results without an
acquired child. The model encodes the documented contract, not the production
state machine line by line.

Histories can be sampled randomly or enumerated exhaustively up to a bound. When
the event alphabet is small, bounded exhaustive enumeration is often better: it
is deterministic, has no seed, and makes a clear claim ("every history up to
length N"). The cost grows exponentially with the bound, so choose the bound
from the contract, for example the longest history that can reach each state
once, rather than from available machine time.

A model can also be checked before any implementation exists. A model checker
such as TLA+'s TLC can explore every interleaving of a small concurrent design,
including separate steps for a fact occurring, being recognized, being queued,
and being dispatched, and can compare candidate designs before a protocol or
lifecycle change is committed. Checking a variant with one safeguard removed
shows which part of the design each property depends on. A model check verifies
the model, not the code: the implementation still needs regressions at the
interleavings the model showed to matter.

A model earns permanent maintenance when the contract is central, stable, and
compact enough that disagreement between model and implementation is itself
useful evidence. A larger model built to explore one redesign is normally
discarded once its counterexamples and design conclusions are recorded as
regressions and architecture.

## Stress, scale, and repeated execution

Stress testing repeats real behavior to raise the chance of observing races,
leaks, scheduler-sensitive failures, or cumulative state corruption. It is
useful when a suspected defect depends on timing or on repeated acquisition and
cleanup and cannot yet be forced through one deterministic interleaving.

Pure repetition explores schedules slowly and reproduces them poorly. A
stronger form uses stand-ins for external components, such as an SSH relay or
a debug server, that pause at named gates. The harness then releases gated
events in seeded orders. A seed reproduces the order at gate granularity, and
the stand-ins record ground truth, such as which input the remote side could
actually read, so the oracle judges the outcome against what actually happened
rather than what was intended. Check for leaked processes, sockets, and files
after each iteration, not only at the end of the run.

Repeated success is weaker evidence than a deterministic invariant, so stress
is discovery or qualification rather than the only permanent protection. When
repetition exposes a failure, capture the event order or resource state that
made it possible and reduce it to a deterministic regression where feasible.
Keep a long-running stress harness only if periodic repetition continues to find
problems deterministic tests cannot express.

Scale is a related axis. Inputs of realistic size, such as a complete
third-party script tree rather than a handful of generated files, can expose
superlinear cost that small generated cases never show. Treat performance as a
finding only against a stated expectation, and keep the regression at the level
of the algorithmic property where possible rather than as a timing assertion.

## Probing real dependencies

Fakes and stand-ins encode assumptions about external components. Exploratory
probes against the real component, in unusual but supported configurations,
test those assumptions. For SSH this includes connection sharing, added
latency, a remote command slow to exit after its input closes, and loss of
different processes in a shared connection. Such probes can reveal behavior no
fake would have modelled, because the fake was written from the same
assumptions as the production code.

Classify each observation as a production defect, an external-component
hazard to document, or a lab artifact. A finding that the maintained external
layers can reproduce deterministically becomes a regression there. Behavior
that the project cannot control, such as an incompatible configuration of a
user's SSH client, belongs in the user documentation or the transport
requirements rather than in a workaround.

## Differential testing

Differential testing runs equivalent inputs through two implementations and
compares observable results. It needs a credible reference and an intent to
remain compatible with it.

The project's main reference is Zephyr's own OpenOCD runner. The maintained
Zephyr integration tests already compare option parsing and generated commands
with it for representative scenarios. A campaign can widen that input space,
for example by generating option combinations. The reference is valuable
because it is maintained independently; a second project-owned implementation
of the same algorithm is a much weaker oracle.

Differential testing also helps when replacing an implementation, for example
an optimization or restructuring. The previous version becomes the reference
for behavior the change is meant to preserve, and generated inputs compare the
two before the old version is deleted.

Every mismatch needs classification. The reference may have behavior this
project intentionally rejects or changes. Normalize only known, documented
differences, and keep scenarios that exercise a deliberate difference out of
the comparison rather than normalizing the difference away; broad normalization
hides regressions. A new intentional difference is a contract decision recorded
in the requirements and in the
[differential acceptance decisions](../traceability/verification.md#differential-acceptance-decisions),
not only in the harness.

## Coverage and decision analysis

Coverage is a lens for choosing targets, not an assurance method by itself.
Statement and branch coverage, described in
[`testing.md`](testing.md#coverage), show code the maintained suite never
executes. Execution alone does not show that a test would detect a wrong
decision; use uncovered or weakly exercised decisions to choose targets for
mutation, generated inputs, or focused regressions.

For a decision with several conditions, a decision table is a practical review
tool: list each combination of conditions that leads to a distinct outcome,
then map every row to a maintained test or to an argument that the row is
unreachable. Rows with neither are the gaps. Formal MC/DC measurement is not
required for this project, but its underlying question is useful: can each
important condition change the outcome while the others are held fixed?
Mutants that replace a single condition with a constant probe the same question
from the other direction.

Do not add tests solely to raise a percentage; that rewards tests of incidental
implementation detail.

## Promoting results

A campaign can finish without changing the repository. That is a valid result
when the stated uncertainty was explored substantially and every observed
failure is explained.

When a production defect is found, keep the fix and the smallest deterministic
regression that protects the violated contract, at an existing maintained
boundary. Do not keep the exploratory harness merely because it found the
problem.

When a generated property is itself a concise, durable statement of the
contract, keeping the property can be better than reducing it to one example.
This suits pure, inexpensive boundaries such as codec round trips and chunking
invariance. Promoting a property that needs a new developer dependency is a
maintenance decision in its own right; a deterministic enumeration, such as
every split point of a small stream, often protects the same contract without
one.

When the investigation exposes an ambiguity in the specification or
architecture, resolve the contract if the distinction is material, updating the
SRS, SAD, or protocol document as the repository guidelines require. Do not
force the implementation or tests to standardize behavior that the project
deliberately leaves unspecified or outside its supported environment.

When the only problem is in the harness, fix it as needed to finish the
investigation and add no product regression for it.

Mutation sets, reference models, generated corpora, stress harnesses, and
campaign-specific instrumentation normally stay in `.scratch/` and may be
deleted afterwards. Their absence from the maintained suite does not erase the
evidence retained as regressions, contract clarifications, and qualification
records.

## Reporting and closure

A campaign report is short enough to review and specific enough to show what
was exercised: the code revision, surfaces in scope, properties or failure
hypotheses, the oracle and how its sensitivity was checked, execution scale
where meaningful, each counterexample and its classification, any regressions
or contract changes, unexplained failures, and the known limits of the
evidence.

Reports belong with the work they justify: in the pull request, issue, or
scratch notes. Seeds, counts, mutant inventories, and individual findings do not
belong in this document; promote only reusable lessons here.

Close the campaign explicitly, stating whether any production defects or
ambiguities remain unexplained and whether further work on the same surface is
justified. Continuing to invent targets after the stated uncertainty is resolved
adds investigation and maintenance cost without adding much confidence.

Explore broadly enough to challenge the implementation, then leave the
maintained repository smaller and clearer than the exploration that produced
the evidence.
