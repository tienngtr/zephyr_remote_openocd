# Bounded lifecycle boundary refinement

Branch: `experiment/architecture-refinement`, from the committed ordering
experiment `88e8a43`. Production baseline: `main` at `ce12b6a`. The previous
experiments and production code, SRS, SAD, and Protocol v1 are unchanged.
`bug` is not the comparison baseline.

## Result and recommendation

The boundary protocol needs six explicit rules: retain recognized facts until
accounted for; establish an admission barrier for success; validate startup
work at execution commitment; transfer a cleanup lease rather than relying on
message receipt; prove producer settlement; distinguish protocol commitment
from delivery. Bounded communication does not invalidate this architecture.
It does require a supervisor that continues draining while other parties wait.

The proposed next step is a **Python component prototype compared with current
main**, not another abstract-model iteration. Test the physical implementation
of these rules using deterministic handshakes, particularly interruption around
acquisition, output admission/spawn entry, and transfer publication. No prototype
or production implementation is included here.

The selected boundary uses a separate bounded critical admission path with
producer-owned prefixes and a generic barrier for independent producers;
bounded bulk relay; generation-scoped effect proposals; and pre-reserved,
single-result cells for executor completions and writer failure. Loop-local
adapters can use atomic recognition/admission instead. Channel separation alone
does not remove the recognition/admission gap or make a fence unnecessary.

## What was modeled and checked

[`BoundaryProtocol.tla`](BoundaryProtocol.tla) refines the previous decision
contracts with concrete bounded mailboxes, command and resource-offer queues,
per-producer barrier request/receipt state, transfer receipt loss, producer
custody release, cleanup response slots, and a partial-writing protocol executor.
It models one remote-helper resource ticket per attempt. Its lifecycle fields
only provide context for those boundaries; address selection, configuration,
real process behavior, forwarding, and RTT behavior are omitted.

This is a boundary refinement model, **not a checked simulation relation to the
entire previous TLA+ module or the production helper**. The previous module has
different event and ticket bounds and combines some steps now separated. The
following abstraction explains the relationship:

| Concrete step/state | Decision-layer meaning |
| --- | --- |
| Mailbox admission, barrier messages, command queueing, writer progress, transfer receipt/status query | Stuttering in lifecycle decisions; fact/ownership progress remains explicit. |
| READY/retry `Success` | The same generic success commit as the ordering experiment, with concrete admission and output-credit guards. |
| Authorized, queued, dispatching | Revocable authorization; dequeue is not execution permission. |
| `StartExecution` | Irreversible producing execution commitment, coupled to local PROCESS_STARTING admission. |
| Acquired/offered | Effect-owned result until the supervisor accepts a cleanup lease. |
| Adopted | Session-owned result; producer may still hold custody pending acknowledgement. |
| Cleaning/cleanup-responded | Outstanding disposal obligation, even after a physical response exists. |
| Disposed/settled | Response accounted for, no live creation/transfer obligation; failed disposal retains a residual lease. |
| Terminal decision followed by writer drain | Abstract closure decision followed by physical delivery progress. |

The generated [checked summary](checked/SUMMARY.md) records the actual TLC
results, specification/tools fingerprints, state counts, and search depths.
It also records TLC's reported fingerprint-collision estimates for complete
runs; graph exhaustion uses finite fingerprints rather than exact state equality.
Trace tables are projections of actual TLC states. Witness configurations negate
a reachability target; their violations demonstrate reachable histories rather
than failed safety. Broken variants must violate the exact named invariant.
Temporal negatives must produce TLC's temporal violation exit code. Tool,
parser, memory, and unrelated invariant failures are rejected.
[`BoundaryWitnesses.tla`](BoundaryWitnesses.tla) adds stricter reachability
targets without changing the protocol: terminal enqueue before writer failure,
late offer delivery, unresolved receipt loss at STOP, retained residual handles,
and a writer-failure cell blocking an otherwise eligible retry.
[`FenceLifetime.tla`](FenceLifetime.tla) separately checks scoped fence completion:
one request belongs to one success proposal, invalidation releases its hold,
and a fair authority eventually decides an acknowledged proposal. This focused
supplement does not change the completed boundary graphs or establish a
compositional refinement proof between the modules.

The safety configurations compare shared-mailbox fencing, separate-path
fencing, and atomic critical admission at capacity one with one bulk fragment
and one generation. Two-generation configurations check retry with no bulk
fragments. The atomic one-generation run additionally includes an independent
FAULT/SIGNAL producer. The temporal runs check a shared-mailbox single-attempt
case and a separate-path retry case, with explicitly narrower bounds. These
are exhaustive finite graphs, not an unbounded proof for arbitrary capacities,
producer counts, resources, message retransmissions, or infinite child output.
No state/depth constraint, simulation, or state-quotient VIEW is used.

| Passing configuration | Generations | Bulk bound | Producers | Control tails / focus |
| --- | --- | --- | --- | --- |
| atomic-urgent | 1 | 1 | control + FAULT/SIGNAL | STOP, EOF, BAD / all |
| shared-fence, urgent-fence | 1 | 1 | control | STOP, EOF, BAD / all |
| retry-atomic, retry-fence | 2 | 0 | control | STOP, EOF, BAD / all |
| fair-closure | 1 | 0 | control | STOP, EOF, BAD / all |
| retry-fair-closure | 2 | 0 | control | STOP / retry |

All queue capacities are one. The retry temporal focus disables READY success
and restricts control-tail recognition to retryable retirement, specifically
probing that boundary. It is not a full two-generation temporal check of every
STOP timing. The two-generation safety configurations retain the full focus.

Histories (`journal`, delivered IDs, required diagnostic records, and `audit`)
are specification evidence, not proposed unbounded runtime buffers. Runtime
capacity consists of bounded admission/data queues, a bounded command queue,
a bounded offer queue, a bounded output queue, one terminal descriptor, one
writer-failure cell, and one result/receipt/query slot per outstanding ticket.
Tombstones and attempt IDs must remain valid while late messages can exist;
arbitrary forgetting or generation reuse is not modeled or authorized.
The control prefix contains at most two facts at these bounds; the optional
guard has one. A concrete producer needs bounded original-fact retention and
must pause before recognizing another fact when that retention credit is used.
Queue capacity counts entries here; the protocol adapter must reserve bytes
for immutable serialized frames and reject oversized admission locally.

## Bounded admission and progress

| Candidate | Checked result and architectural consequence |
| --- | --- |
| A: one shared bounded mailbox, dispatch-only authority | [shared-no-fence](checked/shared-no-fence.md) and [retry-no-fence](checked/retry-no-fence.md) overtake a recognized withheld STOP. This counterexample already refutes an all-observations mailbox design, even granting ideal executor response handling. |
| B: separate small critical queue, still dispatch-only | [urgent-no-fence](checked/urgent-no-fence.md) also fails. Less bulk interference does not serialize recognition with admission. |
| C: producer-owned prefix plus fence, shared or separate ordinary queues | Prefix admission, a hold, continuing supervisor drain, and handling admitted invalidators prevent the overtakes. The passing shared variant includes reserved executor outcome cells; it does not claim that those cells are also transported through the one mailbox. |
| Atomic critical recognition/admission | Wait for admission credit before recognizing a complete fact; append it in that same serialized action. This removes that producer's separate acknowledgement. It is a feasible option for loop-local nonblocking framing, not an assumption about native signal handlers or independent blocking readers. |

Recognition means taking a complete frame/failure as an observed fact. Unread
bytes, incomplete framing, and completed but unexamined physical operations are
not independently modeled. A runtime may not call an already recognized frame
"unrecognized" merely because its put is blocked. Recognition/admission/handling
remain separate for independent producers. Frames preserve source order;
cross-source primary selection is by supervisor commitment, not wall-clock
occurrence. EOF with incomplete framing is BAD, not ordinary EOF.

In C, request IDs increase monotonically. Each producer takes a hold, publishes
its recognized prefix through its normal bounded path, then places its
acknowledgement in its reserved one-slot acknowledgement box. The authority
collects matching receipts. No new recognition is permitted while that producer
is held. Success requires all matching receipts and all admitted facts handled;
commit/abort releases the holds. Old receipts cannot satisfy a later request.
The authority binds each request to the proposed success kind and generation.
Invalidation aborts that request before waiting for retirement cleanup; a later
retry requests a fresh cut. The producer needs the request epoch and prefix,
not knowledge of READY versus retry. Acknowledgement alone is not completion:
the authority must fairly commit or abort and release the hold.
The previous experiment's less restrictive atomic-admission-during-hold option
also remains valid; this refinement chooses pause-through-decision to reduce
the adapter's required handshake cases.
The hold pauses recognition, not physical completion of an external operation.
An adapter that cannot enforce that hold needs equivalent serialized
recognition/admission; it cannot acknowledge and merely hide a newly recognized
result. Native signal synchronization remains a separate physical obligation.

The minimum progress rule is: **waiting for a barrier, command capacity, or
output capacity must not disable admission draining or result accounting**.
No supervisor transition awaits a queue put. Full command queues defer the
proposal; full ordinary protocol output fails locally; result publication uses
capacity reserved before the effect starts. A terminal descriptor can wait
outside the ordinary output queue. Producers/executors make progress separately.

[barrier-cycle](checked/barrier-cycle.md),
[output-cycle](checked/output-cycle.md), and
[effect-cycle](checked/effect-cycle.md) violate `NoBarrierCycle` when the
supervisor disables draining for those waits. This predicate checks an explicit
local dependency cycle, not a claim that every unrelated action in the whole
state graph is disabled. In the barrier case, the held producer cannot complete
its admitted prefix and the authority cannot complete its success decision.
Independent writer or cleanup steps do not unblock that dependency. The good
design keeps its draining action enabled and assumes fair scheduling of it.
The separate [barrier-deadlock](checked/barrier-deadlock.md) run checks
`BarrierProgress` under the otherwise complete fairness assumptions and finds
a repeating barrier-stall execution. This is temporal evidence of the consumer
cycle, beyond the local enabling-condition predicate. It does not rely on a
worker or writer that refuses to respond.

Acknowledgements use reserved slots here. Putting an acknowledgement in the
ordinary full mailbox can also work if the supervisor keeps draining that
mailbox; a separate acknowledgement slot is not mathematically necessary.
It makes the progress obligation independent of bulk output capacity. Its
bound is the number of participating producers, not input volume.

During a shared-mailbox barrier, new bulk publication cannot take credit needed
by an already recognized critical prefix. With held producers that prefix is
finite. In an implementation with indefinitely replenished bulk traffic, credit
reservation or explicit fair/priority admission is required; the finite bulk
bound alone is not a starvation guarantee. Separating critical admission removes
this particular competition, while retaining the fence for recognition gaps.

Writer-failure and ticket-result cells are deliberately different failure
domains. A writer publishes its single immutable failure into permanently
reserved capacity, independently of stdout and the observation mailbox.
`HandleWriterFailure` performs the lifecycle decision later. A cleanup worker
publishes once to its ticket's reserved result slot; `ObserveCleanup` accounts
for it later. These are bounded original fact stores, not copied pending payloads.
A participant that cannot implement such guaranteed admission must instead join
the prefix/hold barrier and retain its original result while blocked.

## Linearization points

STOP becomes ordering-significant at producer recognition. Its terminal intent
linearizes at `Drain`, when the authority handles that ordered fact. A gap
between these points cannot license READY or retry.

READY and retry linearize at `Success`: complete barrier validation, accounting
for admitted facts and pending writer failure, current eligibility/generation
validation, and the phase/generation decision are one serialized action.
READY additionally needs successful local output admission. A readiness
candidate, collected fence receipt, or available command token is not success.
Retry also requires true old-ticket settlement.

The execution linearization point is `StartExecution`: generation/state checks,
PROCESS_STARTING buffer admission, and authorized-to-running commitment form
one local step. Granting a token earlier and executing it later without this
step does not implement the contract.

## Ownership transfer and settlement

Cleanup authority and physical custody are distinct. A generation/ticket-keyed
lease registry belongs to this lifecycle authority. All cleanup decisions,
including producer-side rollback, require a claim accepted by that registry.
Workers do not acquire exclusive cleanup permission from their local belief
about whether a receipt arrived.
Offering pins the descriptor; that pin does not permit continued resource use
or independent disposal after acceptance. The producer consults the same lease
authority before rollback, including when its acceptance receipt is missing.

Those claims describe the model's acquired/offered full handles. Internal
partial initialization before `Acquire` remains in the effect's pre-designated
producer-owned scope. Its physical rollback must not wait for a supervisor
that is joining that producer. The specification does not replace or prove
that native acquisition/rollback scope; it receives its final owned result.

| State | Cleanup authority | Physical custody and progress |
| --- | --- | --- |
| Authorized/queued/dispatching | Producer ticket designated before execution | No acquired handle; terminal revocation can cancel the ticket. |
| Running | Producer | Worker may still create a handle. Timeout does not settle it. |
| Acquired/offered, not accepted | Producer | Producer pins the handle; a queued offer does not transfer ownership. |
| Adoption committed | Supervisor | Supervisor has the descriptor and lease. Producer may pin an alias until receipt/status response. |
| Receipt lost or delayed | Supervisor | The lease remains authoritative. Producer requests status or cleanup; it cannot independently close based on old belief. |
| Cleaning | The one accepted cleanup claimant | Adoption is disabled once cleanup is claimed. No second claimant can independently execute. |
| Cleanup response pending | Existing lease owner | Immutable response remains in its reserved slot until the authority accounts for it. |
| Successful disposal accounted | No physical handle remains | Ticket settlement waits for quiescence and custody release. |
| Failed disposal accounted | Supervisor residual ledger | A potentially unreleased handle remains explicitly owned and accompanied by the complete cleanup diagnostic. It is not silently treated as freed. |

The acknowledgement contract is deliberately small:

1. Offers and receipts identify an immutable generation/ticket; IDs are not reused.
2. Acceptance atomically records the descriptor and transfers the cleanup lease.
   There is no unowned interval.
3. Receipt delivery tells the producer it may release its pin. It does not
   grant cleanup permission, and duplicates cannot change the lease.
4. Lost receipt recovery queries the same authority's durable-in-session lease
   record. A response can confirm supervisor ownership or final disposition.
   It cannot infer ownership from a timeout or absence of a message.
5. A cleanup claim checks the lease and records the sole claimant before any
   disposal starts. Every actor follows this rule, including producer rollback.

The modeled receipt/status action includes pin release and publication of its
final release proof in reserved ticket state. The supervisor cannot infer that
proof merely from sending an acknowledgement. A concrete interruption between
receipt and proof publication leaves custody unsettled until final publication
or a genuine quiescence response establishes release.

This is a same-session protocol. It requires a reachable authority and shared
ticket identity; it is not distributed consensus, a process-crash guarantee, or
a protocol for independent actors that can autonomously close the same handle.
[belief-cleanup](checked/belief-cleanup.md) shows why that autonomy is unsafe.
A two-phase receipt exchange alone cannot remove uncertainty after message loss.
The shared arbiter makes receipt uncertainty harmless to cleanup authority.

In [lost-ack-witness](checked/lost-ack-witness.md), adoption precedes receipt
loss and STOP; the supervisor cleans, and a status exchange releases custody.
[lost-ack-pending-witness](checked/lost-ack-pending-witness.md) additionally reaches
STOP while the producer still awaits release and the supervisor owns the handle.
[delayed-ack-witness](checked/delayed-ack-witness.md) delivers the receipt after
STOP without returning cleanup authority to the producer.
[duplicate-ack-witness](checked/duplicate-ack-witness.md) and
[stale-ack-witness](checked/stale-ack-witness.md) leave current ownership intact.
[owner-gap](checked/owner-gap.md) and [stale-ack](checked/stale-ack.md) demonstrate
the failures when those rules are weakened.

If STOP commits before acquisition/adoption, the eventual result remains
producer-owned and its offer cannot adopt into the retiring attempt.
[late-acquire-witness](checked/late-acquire-witness.md) reaches Closed after
producer-side disposal and response accounting.
[late-offer-witness](checked/late-offer-witness.md) reaches actual offer delivery
after STOP with no adoption and the producer's cleanup lease intact.

True settlement requires all three: proof that the producer can create no
further handle, release of the producer's transferable custody/pin, and final
disposal-response accounting. `Quiesce` means a final no-resource response that
truly excludes future production; `ObserveQuiescence` records it. Acquisition
completion proves no further creation by that bounded operation, but its handle
must still transfer or dispose. Cancellation request and `StopWaiting` prove
neither. Worker termination/reaping may implement a proof of quiescence, but
those OS details are not modeled.

[retry-wait-witness](checked/retry-wait-witness.md) probes retry while a timed-out
producer can still complete; the attempt generation remains unchanged.
[timeout-settles](checked/timeout-settles.md) and
[early-retry](checked/early-retry.md) fail when stopped waiting or unfinished
tickets are treated as settlement. Late harmless command/receipt duplicates may
remain queued after settlement because they cannot create, adopt, or dispose
without a current valid lease. A genuinely live old producer may not cross retry.

Disposal failure is a separate settlement qualification: its final result
establishes terminal failure and preserves a residual ticket, so retry is
forbidden. Conditional Closed means all final attempts/responses are accounted
for, **not that a failed cleanup magically succeeded**. Keeping residual
descriptors reachable is an additional prototype obligation; this model does
not prove current main already retains every such physical handle. Failure
reporting remains compatible with main's best-effort cleanup behavior.
[residual-witness](checked/residual-witness.md) reaches Closed with an explicitly
owned residual handle, operational primary, and the nested cleanup diagnostic.

Cleanup initiation and response publication may occur while another producing
operation is pending. Closure/retry wait for the required ticket settlement;
cleanup initiation itself need not wait. Primary is the established failure;
secondary diagnostics contain retained later failures, including nested records,
without duplicating the primary.

Final producer-side cleanup response accounting also establishes custody
release. That response must be an irrevocable final disposition statement in
the reserved ticket cell, pinning any residual descriptor until the authority
records it. The producer may not resume use or cleanup after publishing it.
If an adapter cannot provide that final publication contract, residual transfer
must use the ordinary offer/receipt/status protocol instead. `ClaimCleanup`
and execution-entry validation are serialized authority operations; their
off-loop rendezvous must use reserved ticket capacity, with admission draining
still enabled. The model does not introduce an unbounded RPC queue for them.
The symbolic cleanup payload includes its nested diagnostic record. A concrete
failure response must snapshot those details at publication; reconstructing it
later from an exception string or mutable notes would not implement retention.
Runtime diagnostic identity must distinguish failure occurrences/tickets;
independent failures with equal prose must not be deduplicated as the primary.

## Effect command and protocol output contracts

Authorization reserves a ticket, not execution. A full effect queue leaves the
proposal in authority-owned state. The supervisor can revoke authorized,
queued, or dequeued-but-not-started work. The executor presents the ticket at
execution entry; the authority checks generation **and current phase/intent**.
Duplicate command delivery cannot repeat execution of an already running or
settled ticket. No cancellation message is needed to establish revocation;
queue draining and ticket cancellation still settle an unstarted obligation.

[revocation-witness](checked/revocation-witness.md) shows STOP with SPAWN still
queued. [generation-only](checked/generation-only.md) starts same-generation
work after STOP when intent/state validation is omitted. Once execution commits,
a late result remains producer-owned even if terminal intent arrives before
physical acquisition. Cleanup is permitted after termination; new unstarted
startup work is not.

| Output stage | Contract |
| --- | --- |
| Ordinary decision/preflight | A proposed event may still fail local admission. |
| Ordinary commitment and buffer admission | Append immutable event identity and reserve FIFO buffer space together. No supervisor wait on a full buffer. |
| Terminal commitment | Reserve one final descriptor independently of ordinary buffer capacity; no later protocol event can commit. |
| Terminal admission | Move that descriptor to the bounded FIFO when credit exists, or record local failure if the writer has failed. |
| Write begins / partial | One queue head is being written; a prefix is not a delivered frame. |
| Write completes | The local writer accepted the whole frame, in order. This does not prove peer receipt or processing. |
| Write fails | Retain the original failure in its reserved cell, abandon further wire output, and account for it locally. |

PROCESS_STARTING is coupled to **combined buffer admission and execution
commitment**, not proposal authorization or queue insertion. A full/failed
buffer causes `RejectSpawnOutput`; no spawn starts and no PROCESS_STARTING
commits ([spawn-reject-witness](checked/spawn-reject-witness.md)). The weaker
[split-spawn-output](checked/split-spawn-output.md) starts without admission.

After admission/execution commitment, the writer may fail before the peer
receives PROCESS_STARTING
([writer-after-spawn-witness](checked/writer-after-spawn-witness.md)). The
already committed effect remains owned and is cleaned. This is an unavoidable
delivery limitation, already present in main; Protocol v1 has no acknowledgement
that could establish peer receipt. Waiting for local write completion would
still not prove peer receipt and would add an output/spawn dependency.

A concrete executor must perform this combined step at actual execution entry,
then enter the spawn attempt immediately without a new queue hop or await.
Granting permission, letting it wait in a worker queue, then spawning would not
preserve the documented immediately-before-attempt meaning. Physical
interruption between enqueue and OS acquisition still needs an owned scope;
this specification is not proof of a Python/native atomic instruction sequence.

The journal models event kinds and attempt identity, not complete JSON payloads.
The writer must receive an immutable serialized frame/snapshot at commitment;
it must not reconstruct an already committed ERROR from a later mutable outcome.
The model checks order and terminal uniqueness, not serializer correctness,
byte-for-byte JSON, descriptor restoration, or stdout peer behavior. Writer
failure after a terminal commitment is retained only in the local outcome. It
is secondary when a primary failure already exists; otherwise it establishes
the local delivery failure. The committed wire decision does not change, and
it cannot publish another ERROR.
[terminal-writer-witness](checked/terminal-writer-witness.md)
preserves operational primary after actual cleanup and ERROR commitment.
[terminal-enqueued-writer-witness](checked/terminal-enqueued-writer-witness.md)
requires ERROR to have actually entered the output buffer before failure; at
capacity one, the failed queue head is then that terminal frame.
[second-terminal](checked/second-terminal.md) and
[lost-diagnostic](checked/lost-diagnostic.md) show the forbidden alternatives.
The failed write may leave a partial final frame; replay is not authorized and
exactly-once delivery is not claimed.

STOP commits orderly SESSION_CLOSED unless cleanup/failure selects ERROR.
Ordinary EOF and signal termination can close without an orderly terminal
frame, matching main. A failure before terminal selection commits ERROR even
when physical delivery is unavailable; its descriptor remains a local decision.
A later delivery failure cannot change that committed wire decision.

## Checked properties and fairness

| Required property | Predicate/checked boundary |
| --- | --- |
| 1–2: recognized terminal fact cannot lose to READY/retry | NoObservedReadyOvertake, NoObservedRetryOvertake; pending writer failure is part of Accounted. |
| 3: no premature acknowledgement under backpressure | NoPrematureBarrierAck. |
| 4: no barrier/consumer dependency cycle | NoBarrierCycle and conditional BarrierProgress; explicit drain fairness. |
| 5–8: ownership, zero-owner gap, exclusive cleanup, stale receipt | ResourceOwned, ExclusiveCleanup, NoStaleTransfer, ResidualOwned. |
| 9–10: old producers settle before retry; timeout is insufficient | RetrySettled, NoFalseSettlement, ClosedSettled. |
| 11–13: revocation, same-generation intent, owned late results | NoInvalidExecution, ResourceOwned, adoption/cleanup guards and checked late-result witnesses. |
| 14–17: ordered protocol, one terminal, no later/second terminal | OrderedOutput, OneTerminalOutput, NoOutputAfterTerminal, SpawnHasAdmission. |
| 18–19: primary and complete diagnostics survive failed delivery | PrimaryPreserved, AllErrorsRetained, WriterDiagnosticOwned, ResidualOwned, NoDuplicatePrimary. |

`TerminationCloses` is conditional on weak fairness of admission draining,
recognized-prefix publication, source hold/receipt processing, writer-failure
accounting, command/offer consumption, cancellation/final producing response,
status query/response, cleanup claim/final response/accounting, ticket settlement,
terminal admission, writer completion-or-failure, and final closure. No fairness
is required for delivery of the lossy transfer receipt: the separate status path
resolves custody. No fairness is assumed for future bytes, successful READY,
future retries, or spontaneous operational failure.

`BarrierProgress` means a requested barrier eventually obtains all receipts or
is released; it does not itself prove that an acknowledged decision finishes.
The focused `FenceEnds` property adds that lifetime obligation under fair
decision scheduling. `NoStaleProposalCommit`, `NoObservedStopSuccess`, and
`NoHoldDuringCleanup` check proposal binding, STOP ordering, and immediate abort
on retirement. [fence-unresolved](checked/fence-unresolved.md) stutters forever
with an acknowledged, held request when decision fairness is removed.
[fence-retarget](checked/fence-retarget.md) reuses an old READY request for retry;
the recognized prefix remains safe, but the request has outlived its proposal.
[fence-supersession-witness](checked/fence-supersession-witness.md) shows a held
READY request aborted by child exit, allowing STOP recognition while cleanup
is still unsettled. These checks add fair resolution without promising future
input or unconditional READY. Terminal intent monotonicity is also checked
temporally. The deliberately weakened
[unfair-admission](checked/unfair-admission.md),
[unfair-workers](checked/unfair-workers.md), and
[unfair-writer](checked/unfair-writer.md) each produce temporal counterexamples.
A worker that never responds, or an unresolvable custody/status exchange, defeats
closure liveness. A bare elapsed timeout is not substituted for those responses.

## Relationship to current main

| Mechanism | Proposed treatment and remaining obligation |
| --- | --- |
| Bounded `_events` | Separate critical admission from bulk relay, or retain sharing with reserved critical credit and ongoing drain. Boundedness remains useful; it cannot make recognized facts disappear. |
| `_pending_control` | Replace its duplicated pending-payload access with the producer's original ordered fact store and generic barrier. A producer-owned fact remains necessary until admission; there is no claim that buffering can be removed. |
| `_ControlFence` | Retain the cut/hold and scoped release contracts; replace helper-specific choreography with monotonic request IDs, matching receipts bound to one proposal, and continuing supervisor accounting. Main's finally-style release has an inherent progress purpose; an acknowledged hold must commit or abort. |
| `_AsyncInput.checkpoint()` | Keep adapter-level final nonblocking scan where required by main. Raw-scan acknowledgement is not a complete-fact admission acknowledgement. The pure model does not prove descriptor scanning or timeout behavior. |
| `_pending_signum` | Retain native safe latching. It is an original recognized fact, not accidental state. Its delivery/recognition and commit synchronization require a native-safe adapter; ordinary asyncio queue operations in a handler are not justified. |
| `_observation_failures` | Replace per-task exception shadow accounting with explicit retained original failure/result cells and structured outcome records. Do not discard guarded failures on queue cancellation or producer join. |
| Child-output observations | Separate bulk fragments from recognized lifecycle facts, preserving stream ordering and required marker decoding. A parser must recognize/retain critical facts before bulk backpressure can hide them. Main's pre-readiness child poll/liveness check remains an adapter obligation, not a guarantee of continued liveness. |
| `_ProtocolOutput` | Retain bounded, nonblocking FIFO output, partial-write accounting, fail-fast enqueue, and final drain. Add an explicit terminal decision/failure domain and original writer-failure cell; preserve local diagnostics on final writer failure. |
| `_spawn_child()` / `SupervisedChild` | Replace decision coupling with ticket authorization and a combined P1-admission/execution-entry rule. Keep physical acquisition rollback, stream/process/group ownership, quiescence/reaping, and identity checks until an adapter supplies equivalent guarantees. |
| `_ForwardManager` pending ownership | Leave its physical scope and interruption-safe pending ownership in the local forwarding lifecycle. A ticket/lease protocol may be reused there later; this model is not a global supervisor for local and remote resources. |
| RTT `_connect()` / adoption | Leave physical socket connection/adoption rollback with the local RTT lifecycle. The same offer/claim semantics can describe it, but the model does not prove socket transfer or Python BaseException safety. |
| Cleanup deadlines | Keep deadlines as policy triggers. Distinguish stopping a wait from final producer quiescence, and failed disposal from released resources. Retain final diagnostics and any residual descriptor; OS escalation/reaping remains physical adapter work. |

This separates failure domains without promising less physical cleanup work.
Protocol event types, pre-spawn meaning, startup STOP/EOF/failure precedence,
retry suppression, terminal uniqueness, and orderly/error outcomes remain the
existing [Protocol v1 contract](../../../docs/architecture/protocol.md).
Queues, tickets, leases, reserved result cells, and receipt IDs are internal
mechanisms, not wire additions. Residual descriptor retention is an explicit
model/prototype ownership discipline, not a newly asserted product requirement
or an already proven property of main.

The [writer-retry-block-witness](checked/writer-retry-block-witness.md) shows why
the permanently reserved writer-failure cell belongs to the same generic
success-accounting rule even before its outcome is handled. The generation,
retry eligibility, and old-ticket settlement are otherwise valid.

## Proposed Python component boundary

Sketch only; these are roles, not a prescribed class count:

```text
ControlAdapter / SignalAdapter / ChildObserver
  original fact retention -> critical admission + SuccessBarrier
  bulk fragments          -> bounded relay admission
                              |
                       LifecycleSupervisor
                       pure decisions + ticket/lease registry
                         /                   \
                  EffectExecutor        ProtocolWriter
                  result/offer cells    bounded FIFO + original failure cell
                  ResourceOffer         local completion/delivery progress
                  TransferAck/status
```

The supervisor has one decision authority and never awaits dependent work while
holding admission progress. It can share an event-loop task with loop-local
nonblocking control framing, child marker decoding, protocol-buffer admission,
and effect execution preflight. Reader/writer coroutines may be independent
tasks on that same loop; they report immutable facts and do not choose outcomes.
The writer needs independent writable-wait progress, not an independent lifecycle
authority. An effect that blocks or runs off-loop needs an independent executor
and a final execution-entry rendezvous; it cannot reuse an old grant blindly.

An independent/blocking control producer needs original-fact retention and the
prefix/hold barrier. A loop-local parser may instead recognize/admit without an
await, with credit reserved before recognition. Native SignalAdapter requires
its safe latch plus a proven commit interaction; this model does not choose
signal masking or call a handler an asyncio producer. ChildObserver must keep
marker/exit/failure observations distinct from bulk relay. Result cells and
cleanup-claim operations can be small supervisor-owned primitives rather than
one task/class for every formal state.
SuccessBarrier is a scoped authority proposal, not a worker wait. Invalidation
releases it before dependent cleanup; acknowledgement enables a fair local
decision and release. READY and retry use the same primitive with different
proposal parameters.

ResourceOffer pins the actual handle and exposes a cleanup descriptor. Supervisor
acceptance records that descriptor and lease together. TransferAck releases the
producer's pin; a status request resolves missing receipt. Cleanup claim and
result publication are ticket-scoped, never inferred from exception precedence.
Local outcomes retain secondary diagnostics and residual resource descriptors
even after terminal wire output becomes immutable or impossible.

Python's semantic difficulty remains at the physical boundaries: task
cancellation at an await and other BaseException interruption can separate
acquisition, publication, and adoption. Cancelling a wait on an off-loop
operation does not prove that its producer stopped. An owned rollback scope,
explicit custody release, and a final completion/quiescence response must make
those obligations real; naming the operations or shielding a wait is insufficient.

Prototype remote-helper components first and compare their behavior with main,
including main's existing START+STOP trace and ordinary/failure terminal forms.
Do not move local forwarding or RTT into that remote authority. Generic ticket
types can be shared without sharing a lifecycle or execution context.

## Reproduction and limits before prototyping

Use the same official TLC 2.19 tools jar (v1.7.4) as the ordering experiment:

```sh
.venv/bin/python experiments/lifecycle/refinement/check.py \
  --jar .scratch/agents/architecture-ordering/tla2tools.jar \
  --reuse --workers 4 --heap 6g
```

`--case` selects configurations. `--reuse` revalidates saved logs only when
specification, configuration, tools, and log hashes match; otherwise TLC runs
again. Raw logs, manifests, tools, and state databases remain ignored scratch
artifacts. Java's local management socket needs local socket permission in this
sandbox. The specification itself uses no actual I/O or clocks.

Counterexample and witness searches use one worker and a 2 GB heap; exhaustive
runs can use the requested parallelism and heap. The checked summary records
the actual resources of each completed run, including reused results. TLC uses
finite-state fingerprinting; these checked bounds are not a mathematical proof
for unbounded executions or a production refinement proof.
New passing temporal searches use TLC's final-graph liveness check to avoid
rechecking growing prefixes; the same complete graph and properties are checked.

The runner verifies 47 expected outcomes: eight passing configurations,
17 deliberately broken safety variants, five temporal counterexamples, and
17 reachable-history witnesses. The largest boundary graph has 145,737,343
distinct fingerprints; TLC reports an actual collision estimate of 0.0011
for that run. The summary preserves both reported estimates rather than
presenting fingerprint exhaustion as an exact theorem.

Repository validation: the ordinary pytest suite passed all 701 tests; the
unchanged previous lifecycle experiment passed its 24 deterministic tests.
`scripts/contributor/static_check.py` also passed, including formatting, typing,
lint, schema/boundary checks, Markdown validation, and diff checks.
No SSH or hardware validation is needed for these pure specifications and
experimental verification tooling.

The remaining implementation obligations are concrete: demonstrate a real
recognition/admission ordering boundary, continuing bounded drain, native-safe
signal interaction, interruption-safe physical handle pin/transfer, final
quiescence evidence, immutable frame construction at actual execution entry,
and local outcome/residual retention after delivery failure. These are reasons
to test a Python structure against corrected main, not to change the protocol,
select another language, or start a further abstract experiment by default.

The transfer receipt/status exchange is additional machinery compared with a
synchronous owned handoff in main. It is justified by independent producer
custody, not by every resource acquisition. Loop-local adoption can combine
lease acceptance and custody release in one serialized call, with the registry
available to rollback if that call is interrupted; queued acknowledgements are
then unnecessary. The prototype should measure decision simplification against
these extra boundary obligations rather than assuming that formal separation
automatically makes the runtime smaller.
