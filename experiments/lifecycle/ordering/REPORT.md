# Admission and effect ordering experiment

Work is on `experiment/architecture-ordering`, created from
`experiment/architecture` at `de50015`. Authoritative production `main` is
`ce12b6a`; `bug` is only the historical counterexample corpus described in the
previous report. No production, SRS, SAD, Protocol v1, or previous experiment
files were changed. This is a finite TLA+ model, not an adapter or a refinement
proof of the deployed helper.

## Findings

READY and retry can share one success rule. A separate producer that may
recognize a terminal fact and withhold admission needs a fence **or equivalent
serialization/communication** before success. Merely making the lifecycle
authority the only state writer does not establish this ordering.

The minimal useful barrier establishes an admitted prefix, prevents a new
recognition/admission gap until the decision, and ensures admitted invalidating
facts are handled before success. It need not prevent all new recognition:
recognition with atomic admission remains safe while acknowledgement is held.

Effect records are revocable, generation-scoped authorizations until execution
commits. Generation validation alone is insufficient after STOP in the same
generation. Protocol output records are irrevocable decisions, with different
semantics from effect authorizations. Neither implies exactly-once physical I/O.

## Model, bounds, and checked results

[`LifecycleOrdering.tla`](LifecycleOrdering.tla) describes Created, Starting,
Active, Retiring, and Closed; attempt generation; terminal intent and a
structured primary/secondary diagnostic outcome; readiness candidate; retry
eligibility; producer/admission/handling prefixes; a barrier; effect ownership
and progress; and protocol/effect journals. Monitor fields in `audit` record
violations or witness coverage and are not proposed implementation state.

There are two ordered sources. Control contains START followed by STOP, EOF, or
a complete invalid/framing-failure fact. A second source supplies SIGNAL or a
session observer failure. Readiness and attempt exit/failure carry generation
tokens and are admitted/handled atomically. They do not duplicate the separate
producer machinery. An attempt exit can be retryable startup failure, fatal
startup failure, or natural exit after Active.

BAD denotes a recognized failure fact, not arbitrary future undecoded input.
Already recognized control/observer failures are retained even when another
terminal intent wins first, and must be accounted for before terminal reporting.
Ordinary control commands encountered after termination can be ignored; a
recognized guarded failure cannot be discarded as though it were such a command.

Safety runs enumerate two generations and two producing tickets per generation
(child and an abstract auxiliary resource). They include single-frame and
two-frame recognition, single-frame handling and atomic START+terminal batch
handling, signal/observer facts, success and failure responses, adoption,
cleanup, nested/additional diagnostics, and stale command/result rejection.
Conditional liveness runs use two generations and one producing ticket per
generation to keep temporal checking small; their safety invariants are checked
too. No state constraints, symmetry quotient, simulation, or depth cutoff are
used. The finite generation/resource/source bounds are real limits of the claim.

[`checked/SUMMARY.md`](checked/SUMMARY.md) contains actual TLC counts for every
configuration. Counterexample/witness Markdown tables are projections of actual
TLC traces, not hand-authored executions. Deliberately broken dispatch histories
restrict recognition ordering to the histories requested in the task; the good
designs have no such restriction. Every negative run must produce its exact
named invariant violation, or the expected temporal violation, with TLC's
corresponding exit code. Parser/tool/runtime failures are never counted as an
expected counterexample.

| Design | Rule | Result |
| --- | --- | --- |
| A: dispatch-only | Drain admitted facts; do not establish a recognized/admitted cut. | Fails for both READY and retry with a recognized, withheld STOP. |
| B: atomic consume/admit | Recognition and admission share one serialized action; success handles the admitted prefix. | All configured safety invariants pass. Conditional closure liveness passes under the stated fairness. No separate acknowledgement is needed. |
| C: admission fence | Acknowledge complete admission, prohibit withholding through the decision, handle admitted facts, then commit or abort. | All configured safety invariants pass. Conditional closure liveness passes. New atomic admission after acknowledgement is permitted. |
| C minus complete admission | Acknowledge despite recognized/unadmitted facts. | `early-ack` fails. |
| C minus hold through decision | Producer can recognize and withhold after acknowledging. | `ack-only` fails. |
| C minus handling admitted facts | Success can commit with an admitted terminal fact still pending. | `no-drain` fails. |

These checks support the finite specification. They are not an unbounded theorem
or a proof that Python/native signal handling implements an atomic action.

Repository validation also passed: 701 ordinary pytest tests, 24 tests for the
previous lifecycle experiment, and the repository static-check command. These
checks do not replace TLC; ordinary pytest discovery does not execute this
formal specification.

## Which observation distinctions matter?

| Stage | Formal representation | Ordering consequence |
| --- | --- | --- |
| Bytes available, unread | An unread source suffix; byte arrival itself is abstracted away. | No obligation to detect unread/future input. |
| Bytes read but not yet a complete observed frame/failure | Also abstracted as unrecognized input. | An incomplete prefix cannot become STOP. EOF with an incomplete frame must produce BAD, not normal EOF. |
| Complete frame or producer failure recognized | `recognized[source]` advances. | This is the observed pending fact point. Terminal facts here must precede a later success commit even when admission is blocked. |
| Fact admitted to authority | `admitted[source]` advances. | Authority owns an ordered inbox fact; it cannot commit success over a pending invalidator. |
| Fact handled/committed | `handled[source]` advances with its lifecycle decision. | STOP establishes monotonic intent. A control failure can establish the primary outcome. |

The first two byte stages are not separate lifecycle states because success
does not depend on them. Recognition versus admission is unavoidable for a
separate producer. Admission versus handling matters here because the inbox may
hold a terminal fact that the authority has not yet processed. An alternative
authority that applies terminal facts during admission can collapse those last
two stages. B cannot collapse them merely by using an ordinary queue.

Recognition is a semantic boundary, not a rename of queue dispatch. In main,
it corresponds to a complete frame taken from `_ControlFrames` (including
framing/read failure recognition), or retained signal/observer failure. JSON
command interpretation may happen later in the authority. A real B design must
serialize complete-frame/failure recognition with admission, not construct a
complete fact first and take an admission lock afterwards. If a runtime already
recognizes all complete frames when reading a chunk, that entire recognized
batch must be included. Raw reading is not permission to relabel a recognized
frame as unrecognized while it waits for queue space.

## Linearization and the minimum fence contract

STOP **becomes ordering-significant at recognition**, even before its lifecycle
transition commits. STOP's lifecycle linearization is the authority action
that handles it and commits terminal intent (`Handle` or `HandleBatch`). READY
and retry linearize at `Success`: final admission/eligibility validation, phase
or generation change, and any associated decision output are one atomic action.
A readiness candidate has no success semantics. An admitted STOP can block
success before its intent transition is dispatched.

The proposed generic rule is:

```text
SuccessCommit(kind, generation):
    establish admission ordering against every participating invalidator source
    handle the admitted relevant prefix
    validate current generation, eligibility, and absence of terminal intent
    atomically commit READY or retry and release the barrier
```

For separate producers the smallest contract demonstrated here is:

1. **Complete prefix:** acknowledgement covers every complete fact already
   recognized by each participating producer. EOF, parsing/read failures, signal
   intent, and recognized observer failures cannot disappear on producer exit.
2. **No renewed withholding:** from acknowledgement until commit/abort,
   recognition either pauses or admits atomically. A new fact admitted during
   this interval must also be handled before success. A one-time high-water mark
   without this hold is insufficient.
3. **Authority accounting:** before success, all admitted invalidators are
   accounted for. Admission alone is insufficient if processing is deferred.
4. **Live scope:** acknowledge the current barrier invocation; success validates
   its generation and eligibility. Commit or abort releases the hold. A stale
   acknowledgement cannot satisfy a later invocation.

The model identifies its live request with generation and purpose and has one
authority/request at a time. Purpose matching is conservative request identity,
not two different ordering protocols. There is no delayed acknowledgement
transport in this model. A reusable implementation needs a fresh request token
or an equally strong no-reuse rule; this specification does not prove safety of
an acknowledgement channel with replay/ABA. Losing eligibility permits abort.

Acknowledge is an aggregate semantic action: every participating source has
fulfilled its prefix/hold promise. It is not a proposal for the coordinator to
inspect every producer's counters atomically. Per-producer acknowledgements,
their delivery, and early holds during collection require a concrete refinement.

One need not implement these obligations using a named fence object: B supplies
the same ordering through atomic recognition/admission. With independent hidden
recognition and no communication, the authority cannot distinguish "no STOP"
from "recognized STOP withheld." Permitting useful success in the former can
permit invalid success in the latter. TLC checks the concrete failures; this
indistinguishability argument explains why some synchronization is necessary,
not why a particular queue/future implementation is mandatory.

B is plausible with serialized nonblocking framing and an authority admission
operation that cannot be blocked by unrelated output capacity. Blocking for
future bytes inside that critical section would break the intended contract.
Bounded raw buffers and ordinary bounded bulk queues are compatible only if
they do not split recognized facts from admission across the success boundary.
Using an unbounded urgent mailbox alone still leaves a race before its append
unless recognition and append share the ordering point. B moves the
synchronization boundary; it does not magically implement it.

C can drain bounded observations while waiting for prefix acknowledgement, or
use a separate ordered admission channel. The model does not choose either.
Acknowledgement cannot wait for the coordinator to stop draining, and the
coordinator cannot stop draining while publication needed for acknowledgement
is blocked. This physical backpressure/deadlock obligation is not proved here.
An idle reader scan is compatible with the barrier but is separate from its
minimum contract: neither acknowledgement nor READY waits for future input.

## Effect and outbox meaning

`effectOutbox` is an immutable record of generation/ticket authorizations.
Executable permission is conditional on current authoritative state. `stage`
records authorized, running, offered/completed, adopted, cleaning, settled, or
cancelled. There is one producing cleanup owner from authorization through
completion; adoption transfers ownership atomically, and cleanup preserves that
owner. Settled means a final no-resource failure or completion of the bounded
disposal attempt, not a guarantee that failed physical cleanup released a handle.

Execution commits at `DispatchEffect`: validity checking and authorized →
running are inseparable. This is the irreversible execution decision. A separate
"check token" followed later by an unprotected "execute" would not implement
this action. After execution commits, terminal intent may arrive before physical
acquisition finishes; completion remains effect-owned and must be disposed.

| Proposed effect semantics | Evaluation |
| --- | --- |
| Irrevocable once authorized | The checked mutant executes an initial SPAWN after STOP. It fails the chosen no-new-producing-execution-after-terminal policy. Ownership could still be retained, so this is not a proof that all irrevocable commands are inherently unsafe; accepting such execution would need an explicit harmlessness exception. It is unnecessary for an undispatched SPAWN. |
| Generation-only authorization | The same-generation STOP trace still executes SPAWN. Fails; a generation token cannot express terminal revocation within its generation. |
| Revocable authorization plus generation/state validation at execution commit | Passes. Terminal/retired state implicitly supersedes the proposal; cancellation settles an unstarted ticket. This is the selected small contract. |
| Command plus cancellation/supersession records | Can implement the same rule, but sending a later cancel command does not itself prevent a racing execute. Still requires ordered acknowledgement or validation at execution commitment. A second cancellation journal is not needed by this model. |

After terminal intent commits:

- An authorized but undispatched producing effect cannot execute. Cancel its
  ticket; do not merely forget it. The immutable proposal can remain in the
  journal with cancelled progress state.
- An effect whose execution decision already committed may finish. This is
  safe only with continued producing ownership, no adoption into a retired/new
  attempt, no READY/retry publication from its result, and closure waiting for
  its final response and disposal. A bare timeout is not proof of settlement if
  the producer can still create a handle later.
- Cleanup/disposal is permitted and necessary after terminal intent. It acts
  on already owned resources, and its failure is retained. It is not a new
  dependent startup operation.

For atomic `ControlBatch(START, STOP)`, the journal may retain START's proposals,
but the final snapshot is terminal and they are not executable. Cancellation
settles them without acquisition. No extra cancellation frame is needed.
**PROCESS_STARTING is committed at child execution commitment, not at proposal
creation.** Protocol v1 associates it with an actual spawn attempt. Therefore
the fully atomic batch path emits no PROCESS_STARTING for a cancelled spawn;
successful close is SESSION_CREATED → SESSION_CLOSED. The alternative ordered
path START handling → spawn commitment → STOP emits PROCESS_STARTING and then
cleans the produced child, as current main does.

This corrects an additional abstraction ambiguity in the previous model, whose
START-side PROCESS_STARTING entry survived proposal cancellation. The new path
preserves the protocol's event meaning; it is not a claim of exact equivalence
to main's current one-child START+STOP integration trace. No production behavior
or contract has been changed. Keeping that exact trace would require committing
the spawn attempt before processing STOP, rather than publishing a revocable
proposal as though spawning already began.

Protocol journal entries are irrevocable, ordered decisions. Once READY commits,
STOP cannot retract it; once a terminal event commits, no later output commits.
Wire buffering, writer failure, output acknowledgement, and replay are outside
this model. Exactly-once delivery does not follow from journal publication.

## Invariants and liveness

Both good safety configurations check these named obligations:

| Requirement | Checked predicate |
| --- | --- |
| 1–2: terminal intent forbids READY/retry | NoReadyAfterTerminal, NoRetryAfterTerminal |
| 3: recognized STOP/failure cannot lose to success | NoObservedReadyOvertake, NoObservedRetryOvertake |
| 4: stale generations cannot commit success/authorize attempts | NoStaleSuccess, NoForbiddenAuthorization, NoInvalidExecution; generation guards at all observation/authorization sites |
| 5: producing resources always have an owner | ResourceOwned, TypeOK |
| 6: retired completion cannot adopt into current attempt | NoStaleAdoption |
| 7–8: one terminal protocol event, no later output | OneTerminalOutput, NoOutputAfterTerminal |
| 9: cleanup preserves established primary | PrimaryPreserved |
| 10: every admitted cleanup failure remains intact | DiagnosticsRetained, comparing the entire nested record |
| 11: terminal state cannot authorize forbidden new work | NoForbiddenAuthorization plus authorization guards |
| 12: invalid proposal cannot execute | NoInvalidExecution |
| 13: retry waits for old producing/disposal obligations | RetryAfterSettlement, ClosedSettled |
| Primary and secondary diagnostics are distinct | NoDuplicatePrimaryDiagnostic |
| Already recognized failure ownership survives terminal selection | RecognizedFailuresRetained and the PendingFailures closure guard |

`FairSpec` checks `TerminationCloses`: committed intent leads eventually to
Closed. It assumes weak fairness of admission/handling for already recognized
facts, cleanup initiation and closure, and for each
ticket cancellation, a final producing response, cleanup dispatch, and a final
cleanup response. Responses may fail; no real clock is modeled. A timeout can
be represented only if it truly settles ownership, not merely stops waiting.
No fairness is assumed for future control bytes or unconditional success/retry.

The unfair variant has a checked stuttering counterexample. A second variant
with coordinator fairness but no worker fairness shows why responding workers
are also necessary. Neither permits an unconditional closure claim. Temporal
checks additionally check terminal intent monotonicity. Two-resource safety and
one-resource temporal checking are separate bounds, not a general liveness proof
for arbitrary resource counts.

Cleanup **initiation** may commit while a producer is still pending. Terminal
closure and retry must wait for all relevant producing/disposal tickets to
settle. This fixes the previous report's overbroad "no close/retry commits"
wording. A first cleanup failure becomes primary only when there is no primary;
later cleanup failures append to secondary diagnostics. The primary is not also
inserted into diagnostics. Nested trees survive in whichever place owns them.

## Required traces

| History | Actual checked artifact/result |
| --- | --- |
| START → recognized STOP withheld → candidate → READY | [dispatch-ready](checked/dispatch-ready.md): READY incorrectly commits with control r/a/h = 2/1/1. |
| Retryable N exit → old cleanup settles → recognized STOP withheld → retry | [dispatch-retry](checked/dispatch-retry.md): generation increases while control admission still lags. |
| START+STOP in one recognized batch | [batch-witness](checked/batch-witness.md): atomic final intent invalidates authorized proposals; no spawn announcement. |
| START authorizes work → STOP commits before dispatch | [pending-effect-witness](checked/pending-effect-witness.md); [irrevocable](checked/irrevocable.md) and [generation-only](checked/generation-only.md) show invalid execution when validation is weakened. |
| Execution begins → STOP → late acquisition result | [late-completion-witness](checked/late-completion-witness.md): producer still owns Offered result; current adoption is disabled. |
| N authorizes work → retry N+1 → late N command/result | [stale-rejection-witness](checked/stale-rejection-witness.md): old dispatch/result rejected without lifecycle/protocol changes. With strict settlement, genuinely outstanding production cannot cross retry; [early-retry](checked/early-retry.md) shows that weakened boundary. Late completions in the good post-retry trace are duplicates, not new physical acquisitions. |
| Operational primary → nested cleanup failure → extra cleanup failure → terminal | [diagnostics-witness](checked/diagnostics-witness.md): operational primary remains; two secondary records including the nested leaf reach ERROR. |

## Protocol requirements versus main mechanisms

The current [protocol document](../../../docs/architecture/protocol.md)
requires no READY over pending startup STOP/EOF/failure, no retry/spawn
announcement after observed pending control termination during old cleanup,
pre-spawn PROCESS_STARTING with its required meaning, proper orderly/failure
terminal events, and no event after a terminal frame. Main's retained
signal/observer failure checks and resource cleanup obligations add authoritative
behavior to preserve. Numeric generations, queues, futures, recursion guards,
and these proposed journals/barriers are mechanisms, not new wire fields.

| Current main mechanism | Classification and formal mapping |
| --- | --- |
| `_pending_control` | Implementation-specific realization of inherent recognized-before-admitted accounting and fact ownership. C uses the producer's original ordered prefix plus acknowledgement; no second lifecycle shadow payload is needed. Retaining the original fact until admission remains mandatory. |
| `_ControlFence` | Implementation-specific realization of an inherent admission cut/hold for separate producers. A named fence is unnecessary under B; some equivalent synchronization remains necessary. Its wake/idle scan and queue/future choreography are not the minimal semantic contract. |
| `_pending_signum` | Native signal recognition/latching realizes an inherent invalidating-fact obligation. Generic interrupt-source accounting represents its ordering. Native reentrancy, safe handler operations, and the physical recognition/commit gap are outside the formal model; the latch is not dismissed as accidental. |
| `_observation_failures` | Retains recognized failure ownership before blocked publication/join. Maps to the second source and intact outcome records. Runtime cancellation and joins remain outside; centralized admission may replace its realization, not the obligation to retain a failure. |
| `_ready()` reconciliation | Accounts for invalidators before success: inherent. Recursive queue drain, reentrant readiness callbacks, and `_reconciling_readiness` are specific to current event handling. A pure non-reentrant success rule removes that recursion machinery, not the need for admission ordering. |
| Retry commit boundary | Inherent settlement + admission + terminal/generation validation. Maps to the same Success operator as READY, with different eligibility. No independent retry ordering protocol is needed. |
| Child identity fencing | Implementation-specific realization of inherent attempt/result fencing. Integer generations are an explicit equivalent, not intrinsically stronger. |
| Pending-forward ownership | Persistent producing ownership and cleanup reachability are inherent and modeled. SIGINT masking, process reaping, rollback-entry protection, and independent physical cleanup execution are outside scope; they cannot be removed based on this model. |
| RTT adoption | Ownership retained until transfer is inherent and modeled. Interruption between socket acquisition, return, adoption, and cleanup is a physical runtime obligation outside scope. |
| Protocol output handling | Ordered committed events and terminal uniqueness are inherent. Journal versus emit is a realization choice. Bounded writer buffering, stdout failure, cancellation/join, final drain, and local diagnostics after wire failure are outside scope. |

The model collapses admission/handling of non-control effect responses and does
not model malformed JSON bytes, actual sockets/processes, exception delivery,
address leasing, workspace release, or writer cleanup. A complete implementation
must account for already recognized facts from all relevant observers, not only
the two representative sources. Do not call those omitted physical obligations
accidental complexity.

## Reproduction and next experiment

Use the [official v1.7.4 tools release](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4)
with TLC 2.19; the published SHA-1 is
`bee4a54f3ee3d4afc347c3240ec2d9e93b075104`. The checked jar's SHA-256 is recorded
in the results. The jar and raw TLC logs/state databases stay in ignored scratch
storage. Run:

```sh
.venv/bin/python experiments/lifecycle/ordering/check.py \
  --jar .scratch/agents/architecture-ordering/tla2tools.jar
```

`--case` selects individual checks. Single-worker, fixed-seed breadth-first TLC
runs produce reviewable experimental Markdown traces and summary; this
command does not commit anything. TLC's local Java management socket needs local
socket permission in this sandbox, although the specification has no real I/O.
Run repository static checks and ordinary pytest separately; pytest discovery
does not invoke TLC.

The next useful experiment is a **formal refinement with bounded admission and
output channels plus an explicit ownership-transfer acknowledgement**. Check
that backpressure cannot hide recognized terminal facts or prevent cleanup, that
barrier acknowledgements cannot be replayed across release/retry, and that a
lost transfer acknowledgement or timeout cannot leave a producer capable of
creating an unowned late handle. Keep it deterministic and independent of any
runtime adapter. Those obligations remain before selecting an implementation
strategy; this result supplies their semantic contract rather than a production
refactor.
