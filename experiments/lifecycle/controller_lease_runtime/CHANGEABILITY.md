# Change locality and invalid states

These are concrete extension sketches against corrected main and this actual
runtime, not source edits to production. No line-count comparison is used.
Main locations are `remote_helper.ControlSession`, its readers/observations,
`SupervisedChild`, `remote.session` and `remote.helper_client`. The experiment
separates decisions from physical source custody but still needs adapter work.

| Change | Current main components/invariants | Candidate 3 components/invariants | Synchronization and possible invalid interleavings | Minimum tests / locality |
| --- | --- | --- | --- | --- |
| Add required output marker | START validation, generated argv, SupervisedChild matcher, output observations, `_ready` evidence and final-reader determination; existing success reconciliation must still cover it | Request marker set and generated invocation; existing bounded Matcher/evidence union/READY rule unchanged | Neither needs new sync for another marker on an owned stream. New independently observed condition needs an adapter/final-scan policy in either design. Failing to tag generation could admit stale evidence | Multi-marker, split streams, EOF completion, deadline evidence. Candidate marker addition is local data change; a new physical source is deliberately not claimed free |
| Add retryable startup cause | Classification, `_finish_attempt`, cleanup/reader settlement, `_retry_commit_boundary`, attempt/address reset, preserved diagnostic paths | Pure provisional-failure classifier plus attempt entry's next argv/address provider; same Settled-to-Authorized rule | Candidate adds no control fence. Both must prove repeating generated startup is safe, retire all prior owned effects and tag late results. Incorrect classifier can repeat non-idempotent Tcl even if lifecycle is perfect | Failed first real process, pending producer, settled retry, old result, EOF before/after authority handling. Candidate decision change local; actual resource classifier remains cross-component |
| Add fatal observer | Task registration, immutable observation plus `_observation_failures`, coordinator handling, readiness reconciliation, cancellation scope | Adapter admits Failure(original diagnostic, optional generation) on independent critical path; central Failure handler unchanged | A blocking or separately recognized observer cannot be assumed atomically admitted; bounded source ownership/progress still required. Revised product semantics remove priority before authority handling, not loss detection. Avoid bulk admission dependency | Observer fails with READY candidate, during termination, after terminal, stale generation; owned cleanup proceeds. More local in candidate; independent physical adapter duties remain |
| Add cleanup-owned resource | Partial initialization/adoption, resource fields, release order, cleanup error composition, interruption rollback and timeout policy | Register physical ownership ticket before publication; disposal worker returns failures and explicit proof; dependent resources wait original completion cells; termination schedules every owner; residual/disposal proof includes it | Source/acquisition-specific interruption protection still needed. Pending producer must block retry/closure but not cleanup initiation of others. Resources with real dependencies cannot be disposed in arbitrary parallel order. A new owner is not automatically appropriate for remote helper: local forwarding and RTT remain separate | Exception before/after return/adoption, late result, one cleanup fails while others succeed, quiescence vs timeout. Candidate terminal decision rule unchanged; physical resource protocol is still substantial |
| Add diagnostic source | Exception selection/precedence, nested note preservation, protocol rendering and client composition | Immutable Diagnostic with nested details, Outcome.fail, writer/boundary rendering | No synchronization to totally order unrelated initial failures. Established primary remains monotonic. Diagnostics after terminal stay local and cannot cause a second terminal message | Primary plus multiple nested secondaries, terminal EPIPE, local transport failure after valid remote result. Candidate data composition is local; schema/rendering require joint revision |

## Hidden-state burden

| Semantic state | Main | Candidate |
| --- | --- | --- |
| Incomplete bytes | `_AsyncInput`/parser | Sole loop parser buffer; inherent |
| Recognized but blocked admission | `_pending_control` plus bounded `_events` | No control producer await between recognition/admission; original fact in critical deque, no second prefix representation |
| Pending native signal | `_pending_signum` plus signal/event queues and success checks | One native latch and OS wakeup; inherent physical adapter state, no remote success fence |
| Observer failure before normal publication | `_observation_failures` plus observation queue | Adapter submits original typed failure independently of bulk; physical worker completion still inspected/reported |
| Pending success fence | `_control_fence`, reached/resume futures | Absent |
| Readiness reconciliation guard | `_reconciling_readiness` | Absent; one authority accounts facts then decides |
| Startup exit/retry state | `_startup_exit`, cleanup flags, ending and retry boundary | Starting.provisional plus attempt variant and ticket final response; safe retry is still explicit |
| Partial output | `_ProtocolOutput` queue/writer/drain state | Writer frame deque, byte count and offset; remains physical |
| Partial custody | SupervisedChild plus construction/rollback protections | Root ticket plus producer/cleanup final response and scope disposition; new explicit registry cost |
| Terminal intent/outcome | Enum plus ending/reason/errors/result fields | Terminating.outcome / Closed.outcome and frozen snapshot |

The bounded critical queue has a source/cardinality limit rather than a
producer-blocking success-barrier protocol. Marker sets/attempt count and parser
input are bounded. Bulk fragments remain in their original per-stream buffers
when writer capacity is exhausted. A production extension with high-volume
critical facts must revisit capacity/source retention; it must not silently
convert the design into an unbounded queue or reintroduce bulk-induced loss.

## Invalid combinations

| Combination | Main prevention | Candidate prevention and remaining obligation |
| --- | --- | --- |
| Terminal plus READY or retry | Procedural ending/state/reconciliation checks | Terminating/Closed have no success data path; current phase still checked at entry; mutation rejects breach |
| Local cancelling plus new GDB/client | Local status and launch coordination | Callback only accepts Opening with both prerequisites; real launch mutation rejected |
| Starting child from wrong attempt | Object identity observation fences | Generational facts plus tagged adoption; Python constructors can still represent a stale Owned ticket, so checked guard remains essential |
| Retry with unresolved old producer/resource | Child/group/output settlement and retry fence | Producing is ineligible, unsuccessful disposal terminal; mutation rejects retry before final response |
| Handle with no cleanup owner | Construction rollback and pending owners | Registry before Popen; original ticket preserved through interruption; lost-registry mutation rejected |
| Two independent cleanup owners | Physical ownership conventions | One root ticket schedules one cleanup task; same-loop adoption needs no independent-custody receipt protocol |
| Natural child status derived from SSH | Separate child/helper result fields and checks | ChildResult constructed only from observed scope exit; independent Shutdown.transport_status |
| Primary cleanup overwrite | Selection rules and composed exceptions | Outcome.fail appends once primary exists; mutation rejects replacement; nested secondary records remain data |
| Terminal then another event | Protocol guards/output conventions | Closed rejects publish; frozen snapshot plus mutable local diagnostics; second-terminal mutation rejected |
| Timeout treated as settlement/disposal | Resource-specific cancellation/cleanup checks | Producer response and disposition proof separate from waiting; local timeout-success mutation rejected |

## Physical changes that remain cross-cutting

New independent asynchronous custody would still require an explicit transfer
receipt/status protocol. This experiment's same-loop Popen handle does not.
New output format must update both sides and rendering; it cannot be hidden in
a decision class. A new process-escape/descendant guarantee would require
physical Linux ownership machinery, not another phase variant. Interactive
GDB signal semantics must be integrated with the local adapter; a generic
LaunchGate alone does not preserve all stock interaction.

The selected design makes the decision invariants local without promising every
feature addition touches one file. Physical ownership and wire compatibility
remain unavoidable multi-component responsibilities.
