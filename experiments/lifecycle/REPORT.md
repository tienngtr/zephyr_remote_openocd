# Lifecycle architecture experiment

This is an executable design probe, not a production implementation or a
protocol proposal. Production, SRS, SAD, and Protocol v1 remain unchanged.
The starting branch already was `experiment/architecture`, at current `main`
`ce12b6a2e7dd00b4b579f70e942c0e5f8ad39bc5`. The historical counterexample is
`bug`, `978ddb62bf3502fcda35df97fe97da95fc5ac739`.

## Baseline inspection recorded before building the model

The five commits from `bug` to `main` are `9d3a4d8`, `07e59eb`, `636de1d`,
`4afd50c`, and `ce12b6a`. Inspection includes their production diffs, current
ownership/coordinator code, matching regression tests, and the current protocol.

| Historical defect and violated invariant | Current `main` enforcement | Nature of enforcement |
| --- | --- | --- |
| Pending forward rollback interrupted on entry, or between A and B: every acquired transport must remain reachable for cleanup, and independent resources must receive cleanup attempts. | `_ForwardManager._pending_processes` is manager-owned from registration through commit/rollback. `close()` merges pending and active ownership without double cleanup; restart rejects stranded pending resources. Registration/commit mask SIGINT; the entire rollback batch masks it and restores the previous mask after attempts. | Structural reachability plus procedural adoption/pop ordering, duplicate ownership reconciliation, signal masking, and exception-note composition. |
| Consumed STOP/EOF/control failure loses to readiness while its bounded queue publication is blocked: already recognized terminal facts must invalidate success. | `_pending_control` retains the fact before `put`; `_ready()` drains queued facts, dispatches pending control, reconciles retained observer failures, and checks state/ending/latched signal. `_reconciling_readiness` prevents recursive readiness publication. Retry additionally uses `_ControlFence` with observer acknowledgement/resume. | Sole coordinator already exists; enforcement additionally depends on shadow state, queue reconciliation, a recursion guard, signal latch, and an explicit retry fence. |
| RTT interruption in initial connection probing or after adoption: acquired sockets must always have a cleanup owner. | `_connect()` catches `BaseException`, closes its socket before propagating, and retains cleanup notes; `run_rtt_client()` enters `try/finally` before calling `_connect()` and adopts into a nullable local. | Procedural protected scopes and local ownership, with exception composition; no general resource transfer type. |
| Nested cleanup details disappear when composing failures: an established primary failure and every retained secondary diagnostic must survive later composition. | `_raise_cleanup_errors`, spawn rollback, session outcome, output drain, signal restoration, and outer control composition copy secondary exception notes as well as summaries, including when a summary already exists. | Distributed exception/notes convention, plus duplicate-summary/detail handling. |

The corrected baseline already fences observations by child object identity,
retains observer failures before blocked publication, owns structured tasks,
and selects terminal failure in the coordinator. Those are comparison baselines,
not improvements attributable to this experiment.

## Result relative to corrected `main`

The model validates a simpler **decision layer**, not yet a simpler complete
implementation. Immutable snapshots combine phase, outcome, ownership, and an
outbox in one publication. This replaces distributed readiness reconciliation
and exception-note composition inside that layer. It does **not** prove that
signal masking, observation admission, or ownership handoff can be removed from
a real Python adapter. A dispatch-only interpretation of observation is
explicitly falsified by a deterministic test.

`main` remains authoritative. The model preserves the tested ordering and
cleanup outcomes at the abstraction described below. It is not a refinement
proof for the full helper, forwarding manager, or RTT client, and its size must
not be compared to production code that also implements I/O and Protocol v1.

## Minimal decision state and inputs

[`model.py`](model.py) contains the executable model. It imports only dataclasses
and enums. [`test_model.py`](test_model.py) exercises real model objects without
effects, mocks of lifecycle collaborators, sleeps, clocks, or scheduling.

The decision state is:

| Field | Necessary distinction |
| --- | --- |
| `phase` | Created, Starting, Active, Retiring, Closed. Retiring includes reversible attempt retirement and terminal cleanup. |
| `generation`, `attempt_limit` | Monotonic attempt ID and retry policy. Tests use two attempts; production's allocation budget is not changed. |
| `ready_candidate` | Readiness evidence exists, but success is not committed. |
| `attempt_failure`, `retryable` | A provisional failed startup can retire without making the session terminal. |
| `outcome` | Optional committed terminal intent: reason, natural return code, optional primary failure, ordered diagnostic trees. Its presence forbids READY/retry. |
| `cleanup_started` | Retirement was decided versus cleanup commands were committed. Needed for interruption before rollback entry. |
| `resources` | Unique `(generation, name)` tickets, progress stage, and cleanup owner. |
| `outbox` | Immutable committed effect commands and semantic protocol outputs, published with the state. |

Every resource has stages Pending → Offered → Held → Cleaning → Done, with
Held optional. Pending and Offered belong to the producing effect. Held belongs
to the session, including resources adopted during startup before readiness.
Cleaning retains the previous owner. Done means either acquisition failed
without a resource or the bounded cleanup attempt finished; it does not assert
that a failed cleanup released the physical resource. Historical Done entries
are retained only to make duplicate/stale completions harmless in this probe.

Immutable inputs are `Command` (START, STOP, control EOF, termination signal,
CLOSE), `ControlFailed`, `ControlBatch`, `Acquire`, `Acquired`, `Adopt`,
`StartupFailed`, `ReadinessCandidate`, `CommitReady`, `ChildExited`,
`StartupTimeout`, `CommitRetry`, and `CleanupFinished`.
Child-spawned is `Acquired(ResourceKey(N, 'child'))`; a spawn failure settles
that pending ticket through `StartupFailed(..., failed_acquisition=child)`.
A bind failure is retryable startup failure or a bind-collision child exit.
Cleanup failure carries a `Failure` tree; there are no exception objects or notes.

STOP/EOF/signal are session-wide. Every attempt-specific observation and commit
request carries a generation. Resource completions additionally carry their
registered ticket. Unregistered or duplicate completions do nothing.

## Transition rules and commit points

`transition(snapshot, event)` prepares a new immutable snapshot. Only
`Supervisor.accept()` publishes it by assigning `_state`. Effects do not run
inside this function, and cannot perform lifecycle transitions themselves.
Readers see the state and its outbox through the same snapshot.

| Input | Rule and committed outputs |
| --- | --- |
| START | Created → Starting, increment generation, register a Pending child ticket, append PROCESS_STARTING and SPAWN. Duplicate START commits protocol failure. Validation/materialization are abstracted as already successful before START. |
| Resource acquisition | Register a ticket and ACQUIRE before an effect runs. On success the effect still owns the offer. Adoption transfers ownership only when the current generation is Starting. |
| Readiness candidate | Record evidence only in current Starting. No protocol output. |
| READY commit | Current Starting + candidate + adopted child + no terminal outcome → Active and PROCESS_READY in the same snapshot. |
| STOP/EOF/signal | Commit an outcome and Retiring, clear candidate. An existing terminal intent/primary remains established. No immediate terminal output. |
| Startup failure/exit/timeout | Retire current Starting. Eligible bind failure remains provisional for retry; timeout, non-retryable failure, or exhausted budget establishes a primary terminal failure. |
| Active child exit | Commit process_exit and its actual return code; subsequent STOP cannot replace it. |
| CLOSE | Commit CLEAN for each Offered/Held ticket, retaining its owner. Pending producing effects remain responsible. Repeated CLOSE does not schedule duplicate cleanup. |
| Acquisition after retirement | Preserve the effect's ownership. If cleanup was committed, append CLEAN rather than adopt. Such completion changes cleanup bookkeeping, never readiness, retry, or the terminal reason. |
| Cleanup completion/failure | Settle that ticket. Append the entire diagnostic tree. Preserve an established primary; absent one, the first cleanup failure becomes primary and forbids retry. |
| Retry commit | Only nonterminal Retiring with eligible failure and every registered effect settled → next Starting and PROCESS_STARTING/SPAWN. |
| All terminal cleanup settled | Closed; append ERROR if there is a primary, otherwise requested/process_exit SESSION_CLOSED. As in current main, EOF/signal alone have no orderly wire close event. |

**STOP linearizes at the supervisor's snapshot publication containing terminal
intent. READY linearizes at publication of Starting → Active plus its outbox
entry.** These are distinct authority decisions, not worker decisions. A
readiness candidate is insufficient. STOP committed between candidate and READY
wins; STOP after READY cannot retract already committed success. Retry has the
same publication rule and terminal guard as READY.

"STOP observed during pending startup" means STOP admitted to the lifecycle
authority before READY commits. This is a precise proposed boundary, but it is
compatible with `main`'s consumed-control guarantee **only if the adapter makes
control consumption and admission inseparable to competing success commits**.
The model does not silently claim that later queue dispatch meets that guarantee.
It does not model byte availability or reader scheduling as lifecycle states.

At readiness, the adapter must also admit already recognized child/observer
failures and current signal intent before offering the commit. No liveness
promise follows the check. Marker parsing, final readiness scans, and child
polling remain effect policies outside the reducer; an expired clock alone is
not necessarily the model's final `StartupTimeout` decision.

## Invariants

1. Only the supervisor publishes mutable session state. Each accepted event is
   a pure decision followed by one snapshot store.
2. Once terminal intent exists, no future READY, retry, or new acquisition
   authorization can commit. Outcome reason and established primary remain fixed;
   diagnostics append in admission order.
3. Generation increases only on initial start or eligible retry. Retired/stale
   attempt facts cannot affect the current lifecycle or protocol outputs.
4. Every authorized producing effect has a registered owner before dispatch.
   Acquisition success keeps effect ownership until adoption commits; rejection
   keeps it through disposal. Every ownership handoff is one snapshot change.
5. No close/retry commits before every producing ticket settles. Independent
   resources all receive cleanup authorization, even if one cleanup fails.
6. Cleanup failure cannot replace an established primary or drop nested details.
7. PROCESS_READY is emitted once at most, in the Active commit. No protocol
   event follows a session-ending event. Effect and output commands are part of
   the same immutable commit as the decisions that authorize them.

These invariants apply to reachable states through the transition API. Python
still permits direct construction of nonsensical dataclass combinations. The
model provides centralized runtime structure, not a type-level impossibility
proof or an interruption-proof runtime.

## Required deterministic histories

Notation: `S` means START followed by successful child acquisition/adoption;
`C`, `P1`, `P2`, `R`, `X`, `E` denote SESSION_CREATED, PROCESS_STARTING for
generation 1/2, PROCESS_READY, SESSION_CLOSED(requested), and ERROR. C is always
the initial outbox entry. Cleanup suffixes include the child plus any extra
registered resources. Outbox output projections are cumulative.

| History | Committed state at decision boundaries | Protocol output projection |
| --- | --- | --- |
| 1. S → candidate → STOP → attempted READY | Starting(candidate) → Retiring(requested); attempted READY does nothing. CLOSE + cleanup → Closed. | C,P1 → C,P1 → C,P1,X |
| 2. S → STOP → candidate → attempted READY | Retiring(requested); both late readiness inputs do nothing. Cleanup → Closed. | C,P1 → C,P1,X |
| 3. S → candidate → READY commit → STOP | Starting(candidate) → Active → Retiring(requested) → Closed after cleanup. | C,P1 → C,P1,R → C,P1,R,X |
| 4. S → retryable exit → cleanup → STOP → possible retry | Retiring(provisional failure) → Retiring(requested); retry cannot increment generation. CLOSE settles → Closed. | C,P1 throughout retirement → C,P1,X |
| 5. N retires → late readiness/exit N → N+1 | Retiring N ignores late facts; after cleanup retry commits Starting N+1. Additional N facts still do nothing. Acquire/adopt/candidate/READY N+1 commits Active. | C,P1 → C,P1,P2 → C,P1,P2,R(generation 2) |
| 6. S + forward A → primary operational failure → two cleanup failures | Retiring(primary operational) → append nested child cleanup detail → append A cleanup detail → Closed. Primary stays operational. | C,P1 → C,P1,E(primary + both complete diagnostic trees) |
| 7. S → begin RTT acquisition → STOP/CLOSE → child cleanup → late RTT acquisition | Retiring(requested) remains pending until the effect-owned RTT offer is disposed. Adoption is rejected; cleanup completion → Closed. | C,P1 throughout late acquisition → C,P1,X |
| 8. S → acquire/adopt A → acquire/adopt B → startup failure → signal before rollback → CLOSE | Retiring(primary startup) before any CLEAN commands; all owners remain reachable. CLOSE schedules child, A, B cleanup together. A failure and another signal cannot abandon B; all completions → Closed. | C,P1 → C,P1,E(primary startup + nested A diagnostic) |

Tests additionally cover acquisition after **attempt** retirement (not just
terminal retirement), retry exhaustion, spawn failure, timeout, active natural
exit versus STOP, EOF/signal, duplicate cleanup, adoption interruption before
commit, interruption after READY commit, and admitted control failure at both
success boundaries. Resource completion after a new generation is deliberately
unreachable here: all producing tickets must settle before retry. Duplicates
from the old generation remain harmless.

History 8 models a signal as a fact following a committed startup failure. In
current main's rollback-entry regression, a reentrant Python SIGINT raises
KeyboardInterrupt before the handler can compose ForwardStartError; close still
finds both resources. The experiment preserves the committed startup primary.
That is a stronger explicit outcome policy at this boundary, **not identical
exception API behavior**. Runtime interruption must be mapped deliberately;
the experiment does not authorize changing that production policy.

## ControlBatch and the remaining fence

An admitted ControlBatch commits all complete consumed control facts before any
effect/output dispatch or READY/retry decision. START+STOP creates the startup
record and then terminal intent in one publication; a synchronous spawn cannot
run between handling those frames. PROCESS_STARTING still appears, matching the
tested main behavior; a child producing ticket must still settle. Cancellation
may settle an unstarted effect without spawning it, but that optimization is
not implemented by this pure model.

This materially simplifies **within-batch** ordering: no retained single
`_pending_control`, queue drain that reenters readiness, or START-side immediate
readiness publication inside batch processing is needed. It also handles a
batch containing a control failure. Batch contents retain frame order; STOP
does not gain priority over earlier failures already committed in the batch.

Batching alone does not order a batch against another observer. The test
`test_withheld_consumed_stop_requires_an_admission_fence` constructs an already
consumed STOP batch outside the authority, commits READY, then admits the batch:
READY appears. That would violate current main's guarantee. Reversing admission
and READY suppresses success. The same counterexample applies to retry.

Therefore an adapter must either:

- Consume/parse/admit control in the authority's serialized execution, without
  a suspension or effect dispatch in between; already consumed batches then
  need no separate acknowledgement.
- Or gate READY and retry with acknowledgement that the control producer has
  admitted all consumed facts and is paused until the decision. Admission must
  not be blocked behind bulk output queue capacity. The gate also accounts for
  already recognized failures and latched signals.

An idle reader scan may still be required at retry or final readiness by the
existing observation policy. Acknowledge that scan/batch admission before the
commit; do not wait for future input. This is the remaining role of main's
retry fence. ControlBatch shortens its obligation but does not eliminate it
when a separate producer can retain unadmitted observations. No proposed new
wire acknowledgement or protocol field is needed.

## Historical defect comparison

| Defect | Pre-fix failure on `bug` | Current `main` mechanism | Experimental mechanism | Simpler/stronger relative to `main`? |
| --- | --- | --- | --- | --- |
| Forward rollback entry/between attempts interrupted | Local pending list is unreachable by close after entry interruption; interruption between cleanups leaves B live/unclosed. | Manager-owned pending set, protected registration/commit, SIGINT-deferred rollback batch, close union/deduplication, incomplete-rollback restart guard. | One persistent ownership ledger; failure commits without detaching resources. CLOSE appends all independent CLEAN commands atomically; interruption cannot erase them. | Simpler in decision state: no active/pending-list merge or mask there. Physical acquisition, task cancellation, and cleanup workers still need protection. Stronger explicit preservation of a primary already committed before the signal; differs from main's exposed rollback-entry exception. |
| STOP/control termination racing READY | Consumed STOP/EOF/invalid frame blocked at queue publication loses to PROCESS_READY. | `_pending_control`, queue drain, readiness recursion guard, observer failure reconciliation, signal latch; acknowledged fence at retry. | STOP commits terminal intent; candidate and READY commit separated; atomic admitted ControlBatch; central generation/terminal guards. | Simpler after admission. **Not stronger by itself** for consumed-but-unadmitted input; an admission gate/fence remains mandatory to match main. |
| RTT connection/adoption interrupted | Socket remains open during initial readiness or after adoption before cleanup scope. | BaseException-safe local connection cleanup, caller cleanup scope established before connection/adoption, retained cleanup notes. | Registered producer owns the resource before/after acquisition until adoption commit. Stale offer stays producer-owned and is disposed, never adopted. | Simpler uniform ownership rule across child/forward/socket. The real Python acquisition/transfer wrapper remains necessary; no proof that this removes its protected scopes. |
| Nested cleanup diagnostics lost | Terminal ERROR retains failure summaries but loses secondary exception's nested notes. | Explicit copying of nested notes at each failure-composition boundary and preservation even when summary exists. | Immutable Failure trees appended to Outcome; terminal outbox carries the complete structured outcome. | Genuine decision-layer simplification: no summary deduplication or notes/exception precedence reconstruction. One adapter conversion and wire rendering still required. |

## Complexity that disappears, remains, or moves

**Disappears inside the decision layer:** scattered emit calls; selecting the
terminal result from protocol/operation errors plus exceptions/notes; duplicate
pending and committed ownership containers; reentrant readiness reconciliation;
procedural checks against ending, latched signal, and shadow control facts at
multiple success sites. One immutable outcome acts as terminal intent. One
ownership ledger covers in-flight, offered, adopted, and cleaning resources.

**Remains inherent:** failed attempt versus terminal failure; attempt fencing;
bounded retry eligibility; pending acquisition versus adopted ownership; resource
settlement before retry/close; cleanup diagnostics; observer shutdown; child
exit/readiness ordering; observation admission before success; resource-specific
cleanup; output ordering and delivery failure. Main's child-identity fencing is
already correct; integers make fencing explicit and easy to generate in tests,
not intrinsically stronger for the one-current-child case.

**Moves to adapters:** recognizing markers/bind collisions, scanning at the
deadline, polling child liveness, framing/validating control, signal delivery,
safe acquisition of actual handles, commit acknowledgement, cancellation/join,
cleanup scheduling and its bounded attempts, exception-tree normalization, and
serializing/rendering ERROR.message. The real wire must keep current fields and
flatten diagnostics without dropping any nested detail. Terminal writer failure
after terminal-frame commitment cannot generate a second terminal frame;
delivery diagnostics must survive in the local structured outcome.

The outbox is part of the snapshot, rather than a transient return value that
could disappear if the caller is interrupted after publication. This makes
decisions reviewable. It is **not** an exactly-once I/O protocol. The probe keeps
its journal indefinitely; bounded queues, consumer cursors, delivery
acknowledgements, idempotent resource command execution, and backpressure remain
unimplemented. Repeated CLOSE is idempotent in the reducer, not proof that a
physical cleanup executor cannot duplicate a command.

The explicit CLOSE decision is a scheduler seam, not a new wire command. A real
supervisor must drive it after retirement and ensure cleanup work runs despite
interruptions. The pure model proves safety for arbitrary orderings, not progress
when a producing effect or cleanup worker never reports a completion. Cancellation
must settle producing tickets, including effects never started; otherwise close
deliberately remains Retiring. Forward/RTT tokens here extend the ownership probe
to the historical local-client cases; they do not propose relocating those
resources into the deployed helper.

SESSION_CREATED assumes outer initialization has succeeded. Workspace, address
lease, output writer, and signal-handler installation are not separate tickets
in this minimal probe. A complete adapter must register their ownership and
settle them before terminal delivery. The generic ledger expresses the ownership
rule, but these tests do not establish full session cleanup or wire equivalence.
Output generation labels and structured outcomes are model metadata, never
proposed extra Protocol v1 fields.

## Semantic Python friction

- Frozen dataclasses and a serialized authority give immutable decision state,
  but Python has no affine/linear ownership type. A physical handle can still be
  copied, closed twice, or forgotten. The registered effect must remain cleanup
  owner until acknowledgement, and afterwards consult authoritative ownership;
  acknowledgment loss must not produce two independent cleanup owners.
- A Python signal may raise BaseException between acquisition and registration,
  between function return and caller adoption, or between physical cleanup and
  diagnostic publication. A pure reducer cannot mask these intervals. The
  producing wrapper must register ownership before acquisition where possible,
  protect the return/adoption boundary, and convert interruption into a fact.
- One reference store publishes this immutable snapshot in the sequential model.
  That is not a general Python transaction or a concurrency guarantee across
  threads/interpreters. Reentrant signal handlers must not call accept; multiple
  producers must enter a serialized authority. A lock around dispatch alone does
  not solve withheld consumed observations.
- Interruption before snapshot publication leaves the old owner/outbox; after
  publication leaves the new owner/outbox. Tests establish those abstract
  boundaries. A crash/reentrant exception during the real resource transfer or
  output write still requires adapter-level recovery, not another reducer flag.
- Arbitrary exceptions, mutable notes, traceback/context graphs, and nested
  exception groups need one deliberate conversion to immutable diagnostics.
  This is more than cosmetic formatting; omit nested notes there and the
  original defect returns at a different boundary.
- An enum plus optional fields does not make invalid state combinations
  unrepresentable. More elaborate tagged variants could do so, but were not
  added merely for stylistic completeness. Mypy also retains narrowing of a
  mutable snapshot property across accept calls; tests read fresh snapshots
  through a helper instead of suppressing type errors.

## Validation and limits

Run the probe explicitly, because repository pytest discovery covers maintained
tests under `tests/`, not this experiment:

```sh
.venv/bin/python -m pytest experiments/lifecycle/test_model.py -q
.venv/bin/python -m pytest -q
.venv/bin/python scripts/contributor/static_check.py
```

The bounded sequence test explores six decisions from Created, adopted Starting,
and failed-attempt cleanup boundaries, using 20 event choices. It merges states
with identical decision data after checking each representative transition's
outbox prefix and invariants: 479 decision states and 7,040 transitions. Explicit
histories supply successful generation-2 readiness and the other requested
longer sequences. This is bounded exploration, not exhaustive verification for
all lengths or effect implementations.

The same 13 existing regression nodes were run against current main and against
an isolated `git archive bug` with the three current regression files overlaid.
They pass on main and all fail on bug at the expected semantic assertions:
unclosed forward processes/pipes, premature PROCESS_READY, still-open RTT
sockets, and missing nested cleanup detail. The tests use real production
objects and real local acquisition boundaries, not the abstract model as a
replacement for production. Their fallback cleanup ran after the assertions.
No SSH, Zephyr, OpenOCD, GDB, or hardware validation was performed.

The initial restricted-sandbox run could not reach some local socket boundaries;
rerunning the existing local regressions and ordinary suite with local socket
permissions succeeded. That initial failure is not counted as evidence against
main. Historical logs and its disposable archive are in ignored
`.scratch/agents/`; no production or maintained test files were modified.

Validation results: 24 experimental tests passed; all 701 ordinary repository
tests passed; repository static checks passed, including the experimental Python
and Markdown files. The model uses no external dependencies. Local validation
used the existing `.venv` (Python 3.14.7, satisfying the repository's 3.12+
requirement). Nothing was committed or pushed.

The decision-layer simplification is supported. The stronger claim that these
principles alone remove admission fences, Python handoff protection, or cleanup
execution machinery is falsified or remains unproven. Selecting an implementation
strategy needs a concrete adapter design for those boundaries; this experiment
does not recommend a production refactor or a language rewrite.
