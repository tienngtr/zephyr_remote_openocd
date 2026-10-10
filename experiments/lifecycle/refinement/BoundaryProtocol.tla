-------------------------- MODULE BoundaryProtocol ---------------------------
\* SPDX-License-Identifier: Apache-2.0
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS ChannelDesign, AdmissionRule, Mutant, MaxGeneration, Capacity,
          BulkLimit, ExtraSource, TailFacts, Focus

Sources == {"control", "guard"}
Generations == 1..MaxGeneration
None == "none"
TerminalKinds == {"ERROR", "SESSION_CLOSED"}
\* Facts is defined below against the state; source payloads never change.
Entry(kind, g) == [kind |-> kind, generation |-> g]
Message(source, index) == [source |-> source, index |-> index]
Error(code, g) == [code |-> code, generation |-> g,
                  nested |-> IF code = "cleanup" THEN <<"close-detail">> ELSE <<>>]
NoError == Error(None, 0)

VARIABLE st
vars == <<st>>
Facts(source) == IF source = "control" THEN <<"START", st.tail>> ELSE <<st.guardFact>>
Participants == IF ExtraSource THEN Sources ELSE {"control"}
Lane(source) == IF ChannelDesign = "shared" \/ source # "bulk" THEN "critical" ELSE "bulk"
Room(source) == Len(st.mailboxes[Lane(source)]) < Capacity
Gap == \E source \in Participants : st.recognized[source] > st.admitted[source]
ObservedTerminal ==
    \E source \in Participants : \E i \in 1..st.recognized[source] : Facts(source)[i] # "START"
PendingFailure ==
    \E source \in Participants : \E i \in (st.handled[source] + 1)..st.recognized[source] :
        Facts(source)[i] \in {"BAD", "FAULT"}
OutputRoom == Len(st.output) < Capacity /\ ~st.writerFailed
ValidEffect(g) == g = st.generation /\ st.phase = "Starting" /\ st.intent = None
TicketSettled(g) == st.stage[g] \in {"absent", "cancelled", "settled"}
                   /\ st.quiet[g] /\ st.released[g]
                   /\ (~st.resource[g] \/ st.residual[g])
OldSettled == \A g \in Generations : g <= st.generation => TicketSettled(g)
TerminalCommitted == \E i \in 1..Len(st.journal) : st.journal[i].kind \in TerminalKinds
BarrierComplete ==
    IF AdmissionRule = "fence"
       THEN st.barrier = "requested" /\ \A source \in Participants : st.receipts[source] = st.barrierId
       ELSE TRUE
Accounted == st.handled = st.admitted /\ ~st.writerPending
SuccessEligible(kind) ==
    /\ st.intent = None
    /\ IF kind = "ready" THEN st.phase = "Starting" /\ st.candidate
                                  /\ st.stage[st.generation] = "adopted"
          ELSE st.phase = "Retiring" /\ st.retry /\ st.generation < MaxGeneration
               /\ (Mutant = "early-retry" \/ OldSettled)

\* All failure records admitted to this model are retained independently of
\* stdout. Terminal delivery failures stay local and cannot report again.
Fail(state, error) ==
    [state EXCEPT
       !.primary = IF @ = NoError THEN error ELSE @,
       !.diagnostics = IF state.primary # NoError THEN Append(@, error) ELSE @,
       !.requiredErrors = Append(@, error),
       !.intent = IF @ = None THEN "failure" ELSE @,
       !.phase = IF @ = "Closed" THEN @ ELSE "Retiring",
       !.retry = FALSE, !.candidate = FALSE]
AppendProtocol(state, kind, g) ==
    [state EXCEPT !.journal = Append(@, Entry(kind, g)),
                  !.output = Append(@, Len(state.journal) + 1),
                  !.admittedOutput = Append(@, Len(state.journal) + 1)]

Init ==
    \E tail \in TailFacts,
       guardFact \in (IF ExtraSource THEN {"FAULT", "SIGNAL"} ELSE {"FAULT"}) :
    st = [phase |-> "Created", generation |-> 0, intent |-> None,
       primary |-> NoError, diagnostics |-> <<>>, requiredErrors |-> <<>>,
       tail |-> tail,
       guardFact |-> guardFact, candidate |-> FALSE, retry |-> FALSE,
       recognized |-> [s \in Sources |-> 0], admitted |-> [s \in Sources |-> 0],
       handled |-> [s \in Sources |-> 0],
       mailboxes |-> [lane \in {"critical", "bulk"} |-> <<>>], bulkCount |-> 0,
       barrier |-> "idle", barrierId |-> 0,
       holds |-> [s \in Sources |-> 0], receipts |-> [s \in Sources |-> 0],
       ackBoxes |-> [s \in Sources |-> 0],
       stage |-> [g \in Generations |-> "absent"],
       owner |-> [g \in Generations |-> None], resource |-> [g \in Generations |-> FALSE],
       quiet |-> [g \in Generations |-> TRUE], released |-> [g \in Generations |-> TRUE],
       timedOut |-> [g \in Generations |-> FALSE], adopted |-> [g \in Generations |-> FALSE],
       cleanupActor |-> [g \in Generations |-> None], offers |-> <<>>, commands |-> <<>>,
       cleanupResult |-> [g \in Generations |-> "empty"],
       residual |-> [g \in Generations |-> FALSE],
       transferAck |-> [g \in Generations |-> "empty"],
       ackLost |-> [g \in Generations |-> FALSE], ackDuplicate |-> [g \in Generations |-> FALSE],
       statusQuery |-> [g \in Generations |-> FALSE], staleAckSeen |-> FALSE,
       commandReplay |-> [g \in Generations |-> FALSE],
       journal |-> <<Entry("SESSION_CREATED", 0)>>, output |-> <<1>>,
       admittedOutput |-> <<1>>, delivered |-> <<>>, writerStage |-> "idle",
       writerFailed |-> FALSE, writerPending |-> FALSE, writerFault |-> NoError,
       terminalPending |-> 0, terminalChosen |-> FALSE,
       audit |-> [readyOvertake |-> FALSE, retryOvertake |-> FALSE,
          invalidExecution |-> FALSE, ackGap |-> FALSE, earlyRetry |-> FALSE,
          twoCleaners |-> FALSE, staleTransfer |-> FALSE, spawnWithoutAdmission |-> FALSE,
          blockedStop |-> FALSE, lateAcquired |-> FALSE, retryRejected |-> FALSE,
          ackLostBeforeTerminal |-> FALSE, ackAfterTerminal |-> FALSE,
          spawnEnqueueRejected |-> FALSE, terminalWriterFailed |-> FALSE,
          primaryReplaced |-> FALSE]]

\* Atomic recognition waits for credit BEFORE recognizing a complete fact.
\* Fence producers retain the original fact and stop recognition when held.
Recognize(source) ==
    /\ source \in Participants /\ st.phase # "Closed"
    /\ st.recognized[source] < Len(Facts(source))
    /\ st.holds[source] = 0
    /\ (AdmissionRule # "atomic" \/ Room(source))
    /\ (Focus # "retry" \/ source # "control" \/ st.recognized[source] # 1
         \/ (st.phase = "Retiring" /\ st.retry))
    /\ st' = [st EXCEPT !.recognized[source] = @ + 1,
         !.admitted[source] = IF AdmissionRule = "atomic" THEN st.recognized[source] + 1 ELSE @,
         !.mailboxes[Lane(source)] = IF AdmissionRule = "atomic"
             THEN Append(@, Message(source, st.recognized[source] + 1)) ELSE @]

PublishRecognized(source) ==
    /\ source \in Participants /\ st.phase # "Closed"
    /\ st.admitted[source] < st.recognized[source] /\ Room(source)
    /\ st' = [st EXCEPT !.admitted[source] = @ + 1,
         !.mailboxes[Lane(source)] = Append(@, Message(source, st.admitted[source] + 1))]

PublishBulk ==
    /\ st.generation > 0 /\ st.phase # "Closed" /\ st.bulkCount < BulkLimit /\ Room("bulk")
    /\ st.stage[st.generation] \in {"running", "acquired", "offered", "adopted"}
    \* Reserve released admission credit for recognized critical facts during
    \* the barrier. Finite checked bulk bounds are not a starvation theorem.
    /\ st.barrier # "requested" \/ ~Gap
    /\ st' = [st EXCEPT !.bulkCount = @ + 1,
         !.mailboxes[Lane("bulk")] = Append(@, Message("bulk", st.generation))]

CanDrain ==
    /\ ~(Mutant = "freeze-drain" /\ st.barrier = "requested")
    /\ ~(Mutant = "await-output" /\ ~OutputRoom)
    /\ ~(Mutant = "await-effect" /\ Len(st.commands) = Capacity)

Drain(lane) ==
    /\ lane \in {"critical", "bulk"} /\ Len(st.mailboxes[lane]) > 0 /\ CanDrain
    /\ st.phase # "Closed"
    /\ LET message == Head(st.mailboxes[lane])
           base == [st EXCEPT !.mailboxes[lane] = Tail(@)]
           fact == IF message.source = "bulk" THEN "bulk"
                      ELSE Facts(message.source)[message.index]
           accounted == IF fact = "bulk" THEN base
                        ELSE [base EXCEPT !.handled[message.source] = message.index]
       IN st' = CASE fact = "START" /\ st.phase = "Created" /\ st.intent = None ->
             [accounted EXCEPT !.phase = "Starting", !.generation = 1,
                 !.stage[1] = "authorized", !.owner[1] = "producer", !.released[1] = FALSE]
          [] fact \in {"BAD", "FAULT"} -> Fail(accounted, Error(fact, st.generation))
          [] fact \in {"STOP", "EOF", "SIGNAL"} ->
             [accounted EXCEPT !.intent = IF @ = None THEN fact ELSE @,
                 !.phase = "Retiring", !.candidate = FALSE, !.retry = FALSE]
          [] fact = "bulk" /\ ~st.terminalChosen /\ ~st.writerFailed ->
             IF OutputRoom THEN AppendProtocol(accounted, "CHILD_OUTPUT", message.index)
                           ELSE Fail(accounted, Error("output-backlog", st.generation))
          [] OTHER -> accounted

BeginBarrier ==
    /\ AdmissionRule = "fence" /\ st.barrier = "idle" /\ st.barrierId < MaxGeneration
    /\ (SuccessEligible("ready") \/ SuccessEligible("retry"))
    /\ st' = [st EXCEPT !.barrier = "requested", !.barrierId = @ + 1,
       !.audit.blockedStop = @ \/ (st.recognized["control"] = 2
          /\ st.admitted["control"] = 1 /\ ~Room("control") /\ st.candidate)]
HoldProducer(source) ==
    /\ source \in Participants /\ st.barrier = "requested" /\ st.holds[source] = 0
    /\ st' = [st EXCEPT !.holds[source] = st.barrierId]
PublishBarrierAck(source) ==
    /\ source \in Participants /\ st.barrier = "requested"
    /\ st.holds[source] = st.barrierId /\ st.ackBoxes[source] = 0
    /\ st.receipts[source] # st.barrierId
    /\ (Mutant = "early-ack" \/ st.recognized[source] = st.admitted[source])
    /\ st' = [st EXCEPT !.ackBoxes[source] = st.barrierId,
       !.audit.ackGap = @ \/ st.recognized[source] # st.admitted[source]]
ReceiveBarrierAck(source) ==
    /\ source \in Participants /\ st.ackBoxes[source] # 0
    /\ st' = [st EXCEPT !.receipts[source] =
                 IF st.barrier = "requested" /\ st.ackBoxes[source] = st.barrierId
                    THEN st.barrierId ELSE @,
               !.ackBoxes[source] = 0]
ReleaseBarrier ==
    /\ st.barrier = "requested" /\ st.intent # None
    /\ st' = [st EXCEPT !.barrier = "idle", !.holds = [s \in Sources |-> 0]]

Success(kind) ==
    /\ kind \in {"ready", "retry"} /\ SuccessEligible(kind) /\ BarrierComplete /\ Accounted
    /\ (Focus = "all" \/ Focus = kind)
    /\ (kind = "retry" \/ OutputRoom)
    /\ LET base == [st EXCEPT
            !.phase = IF kind = "ready" THEN "Active" ELSE "Starting",
            !.generation = IF kind = "retry" THEN @ + 1 ELSE @,
            !.candidate = FALSE, !.retry = FALSE, !.barrier = "idle",
            !.holds = [s \in Sources |-> 0],
            !.audit.readyOvertake = @ \/ (kind = "ready" /\ ObservedTerminal),
            !.audit.retryOvertake = @ \/ (kind = "retry" /\ ObservedTerminal),
            !.audit.earlyRetry = @ \/ (kind = "retry" /\ ~OldSettled)]
       IN st' = IF kind = "ready" THEN AppendProtocol(base, "PROCESS_READY", st.generation)
                ELSE [base EXCEPT !.stage[base.generation] = "authorized",
                                 !.owner[base.generation] = "producer",
                                 !.released[base.generation] = FALSE]
ReadyCandidate ==
    /\ st.phase = "Starting" /\ st.intent = None /\ ~st.candidate
    /\ st.stage[st.generation] = "adopted"
    /\ (Focus # "ready" \/ st.recognized["control"] = 2)
    /\ st' = [st EXCEPT !.candidate = TRUE]

PublishCommand(g) ==
    /\ g \in Generations /\ st.stage[g] = "authorized" /\ ValidEffect(g)
    /\ Len(st.commands) < Capacity
    /\ st' = [st EXCEPT !.commands = Append(@, g), !.stage[g] = "queued"]
TakeCommand ==
    /\ Len(st.commands) > 0
    /\ LET g == Head(st.commands) IN st' = [st EXCEPT !.commands = Tail(@),
         !.stage[g] = IF @ = "queued" THEN "dispatching" ELSE @]
ReplayCommand(g) ==
    /\ g \in Generations /\ st.stage[g] \in {"adopted", "cleaning", "disposed", "settled"}
    /\ ~st.commandReplay[g] /\ Len(st.commands) < Capacity
    /\ st' = [st EXCEPT !.commands = Append(@, g), !.commandReplay[g] = TRUE]
CancelCommand(g) ==
    /\ g \in Generations /\ st.stage[g] \in {"authorized", "queued", "dispatching"}
    /\ ~ValidEffect(g)
    /\ st' = [st EXCEPT !.stage[g] = "cancelled", !.released[g] = TRUE, !.owner[g] = None]

\* An unprotected dispatch check or a grant before P1 admission is insufficient.
StartExecution(g) ==
    /\ g \in Generations /\ st.stage[g] = "dispatching"
    /\ IF Mutant = "generation-only" THEN g = st.generation ELSE ValidEffect(g)
    /\ OutputRoom \/ Mutant = "split-spawn-output"
    /\ LET base == [st EXCEPT !.stage[g] = "running", !.quiet[g] = FALSE,
         !.audit.invalidExecution = @ \/ ~ValidEffect(g),
         !.audit.spawnWithoutAdmission = @ \/ ~OutputRoom]
       IN st' = IF OutputRoom THEN AppendProtocol(base, "PROCESS_STARTING", g)
                ELSE [base EXCEPT !.journal = Append(@, Entry("PROCESS_STARTING", g))]
RejectSpawnOutput(g) ==
    /\ g \in Generations /\ st.stage[g] = "dispatching" /\ ValidEffect(g) /\ ~OutputRoom
    /\ st' = [Fail(st, Error("spawn-output", g)) EXCEPT !.audit.spawnEnqueueRejected = TRUE]
ReadyOutputFailure ==
    /\ SuccessEligible("ready") /\ BarrierComplete /\ Accounted /\ ~OutputRoom
    /\ st' = Fail(st, Error("ready-output", st.generation))

Acquire(g) ==
    /\ g \in Generations /\ st.stage[g] = "running"
    /\ st' = [st EXCEPT !.stage[g] = "acquired", !.resource[g] = TRUE,
       !.quiet[g] = TRUE, !.audit.lateAcquired = @ \/ st.intent # None]
Quiesce(g) ==
    /\ g \in Generations /\ st.stage[g] = "running"
    /\ st.intent # None \/ st.phase = "Retiring"
    /\ st' = [st EXCEPT !.stage[g] = "quiescent", !.quiet[g] = TRUE,
                        !.released[g] = TRUE, !.owner[g] = None]
ObserveQuiescence(g) ==
    /\ g \in Generations /\ st.stage[g] = "quiescent"
    /\ st' = [st EXCEPT !.stage[g] = "settled"]
StopWaiting(g) ==
    /\ g \in Generations /\ st.stage[g] \in {"running", "acquired", "offered", "adopted"}
    /\ ~st.timedOut[g]
    /\ st' = [st EXCEPT !.timedOut[g] = TRUE,
         !.stage[g] = IF Mutant = "timeout-settles" THEN "settled" ELSE @,
         !.released[g] = IF Mutant = "timeout-settles" THEN TRUE ELSE @]
PublishOffer(g) ==
    /\ g \in Generations /\ st.stage[g] = "acquired" /\ Len(st.offers) < Capacity
    /\ st' = [st EXCEPT !.stage[g] = "offered", !.offers = Append(@, g)]
ReceiveOffer ==
    /\ Len(st.offers) > 0
    /\ LET g == Head(st.offers)
           adopt == st.stage[g] = "offered" /\ ValidEffect(g)
       IN st' = [st EXCEPT !.offers = Tail(@),
          !.stage[g] = IF adopt THEN "adopted" ELSE @,
          !.owner[g] = IF adopt THEN IF Mutant = "owner-gap" THEN None ELSE "supervisor" ELSE @,
          !.adopted[g] = @ \/ adopt,
          !.transferAck[g] = IF adopt THEN "pending" ELSE @]
LoseTransferAck(g) ==
    /\ g \in Generations /\ st.transferAck[g] = "pending" /\ ~st.ackLost[g]
    /\ st' = [st EXCEPT !.transferAck[g] = "lost", !.ackLost[g] = TRUE,
         !.audit.ackLostBeforeTerminal = @ \/ st.intent = None]
ReceiveTransferAck(g) ==
    /\ g \in Generations /\ st.transferAck[g] = "pending"
    /\ st' = [st EXCEPT !.transferAck[g] = "received", !.released[g] = TRUE,
         !.audit.ackAfterTerminal = @ \/ st.intent # None]
DuplicateTransferAck(g) ==
    /\ g \in Generations /\ st.transferAck[g] = "received" /\ ~st.ackDuplicate[g]
    /\ st' = [st EXCEPT !.ackDuplicate[g] = TRUE]
RequestStatus(g) ==
    /\ g \in Generations /\ st.quiet[g] /\ ~st.released[g] /\ ~st.statusQuery[g]
    /\ st.owner[g] # "producer"
    /\ st' = [st EXCEPT !.statusQuery[g] = TRUE]
ReceiveStatus(g) ==
    /\ g \in Generations /\ st.statusQuery[g]
    /\ st' = [st EXCEPT !.statusQuery[g] = FALSE, !.released[g] = TRUE]
StaleTransferAck ==
    /\ st.generation > 1 /\ st.adopted[st.generation - 1] /\ ~st.staleAckSeen
    /\ st' = [st EXCEPT !.staleAckSeen = TRUE,
       !.owner[st.generation] = IF Mutant = "stale-ack" THEN "supervisor" ELSE @,
       !.audit.staleTransfer = @ \/ Mutant = "stale-ack"]

ClaimCleanup(g) ==
    /\ g \in Generations /\ st.stage[g] \in {"acquired", "offered", "adopted"}
    /\ st.phase = "Retiring" \/ g # st.generation
    /\ st.owner[g] \in {"producer", "supervisor"} /\ st.cleanupActor[g] = None
    /\ st' = [st EXCEPT !.cleanupActor[g] = st.owner[g], !.stage[g] = "cleaning"]
BeliefCleanup(g) ==
    /\ Mutant = "belief-cleanup" /\ g \in Generations
    /\ st.owner[g] = "supervisor" /\ ~st.released[g] /\ st.intent # None
    /\ ~st.audit.twoCleaners
    /\ st' = [st EXCEPT !.audit.twoCleaners = TRUE]
FinishCleanup(g, failed) ==
    /\ g \in Generations /\ st.stage[g] = "cleaning" /\ failed \in BOOLEAN
    \* Physical completion publishes to the pre-reserved ticket result slot.
    \* A failure does NOT assert that disposal released the physical handle.
    /\ st' = [st EXCEPT !.stage[g] = "cleanup-responded",
       !.cleanupResult[g] = IF failed THEN "failed" ELSE "ok",
       !.resource[g] = failed]
ObserveCleanup(g) ==
    /\ g \in Generations /\ st.stage[g] = "cleanup-responded"
    /\ LET base == [st EXCEPT !.stage[g] = "disposed",
         !.resource[g] = st.cleanupResult[g] = "failed",
         !.owner[g] = IF st.cleanupResult[g] = "failed" THEN "supervisor" ELSE None,
         !.residual[g] = st.cleanupResult[g] = "failed",
         !.released[g] = @ \/ st.cleanupActor[g] = "producer"]
           result == IF st.cleanupResult[g] = "failed" THEN Fail(base, Error("cleanup", g)) ELSE base
       IN st' = [result EXCEPT !.audit.primaryReplaced = @ \/
                    (st.primary # NoError /\ result.primary # st.primary)]
SettleTicket(g) ==
    /\ g \in Generations /\ st.stage[g] = "disposed" /\ st.quiet[g] /\ st.released[g]
    /\ st' = [st EXCEPT !.stage[g] = "settled"]
RetireAttempt ==
    /\ st.phase = "Starting" /\ st.intent = None /\ st.generation < MaxGeneration
    /\ st.stage[st.generation] \in {"running", "acquired", "offered", "adopted"}
    /\ st' = [st EXCEPT !.phase = "Retiring", !.retry = TRUE, !.candidate = FALSE]
RetryProbe ==
    /\ st.phase = "Retiring" /\ st.retry /\ st.timedOut[st.generation]
    /\ ~OldSettled /\ ~st.audit.retryRejected
    /\ st' = [st EXCEPT !.audit.retryRejected = TRUE]
OperationalFailure ==
    /\ st.phase \in {"Starting", "Active"} /\ st.intent = None
    /\ st' = Fail(st, Error("operational", st.generation))

BeginWrite ==
    /\ ~st.writerFailed /\ st.writerStage = "idle" /\ Len(st.output) > 0
    /\ st' = [st EXCEPT !.writerStage = "writing"]
PartialWrite ==
    /\ ~st.writerFailed /\ st.writerStage = "writing"
    /\ st' = [st EXCEPT !.writerStage = "partial"]
CompleteWrite ==
    /\ ~st.writerFailed /\ st.writerStage \in {"writing", "partial"}
    /\ st' = [st EXCEPT !.delivered = Append(@, Head(st.output)),
                        !.output = Tail(@), !.writerStage = "idle"]
WriterFailure ==
    /\ ~st.writerFailed /\ st.writerStage \in {"writing", "partial"}
    \* Recognition/admission to a permanently reserved one-result cell.
    \* No lifecycle decision is made by this physical writer step.
    /\ st' = [st EXCEPT !.writerFault = Error("writer", st.generation),
       !.writerPending = TRUE, !.writerFailed = TRUE, !.writerStage = "failed",
       !.output = <<>>, !.terminalPending = 0]
HandleWriterFailure ==
    /\ st.writerPending
    /\ LET error == st.writerFault
           retained == IF Mutant = "lose-diagnostic" /\ TerminalCommitted
                          THEN [st EXCEPT !.requiredErrors = Append(@, error)] ELSE Fail(st, error)
       IN st' = [retained EXCEPT !.writerPending = FALSE,
          !.journal = IF Mutant = "second-terminal" /\ TerminalCommitted
                         THEN Append(@, Entry("ERROR", st.generation)) ELSE @,
          !.audit.terminalWriterFailed = @ \/ TerminalCommitted]

\* The terminal outcome reserves one dedicated descriptor, independent of
\* stdout capacity. It is committed once, even if it can never be delivered.
CommitTerminal ==
    /\ st.phase = "Retiring" /\ st.intent # None /\ OldSettled
    /\ ~PendingFailure /\ ~st.writerPending
    /\ st.mailboxes = [lane \in {"critical", "bulk"} |-> <<>>]
    /\ ~st.terminalChosen
    /\ LET kind == IF st.primary # NoError THEN "ERROR"
                     ELSE IF st.intent = "STOP" THEN "SESSION_CLOSED" ELSE None
       IN st' = [st EXCEPT !.terminalChosen = TRUE,
          !.journal = IF kind = None THEN @ ELSE Append(@, Entry(kind, st.generation)),
          !.terminalPending = IF st.writerFailed \/ kind = None THEN 0 ELSE Len(st.journal) + 1,
          !.barrier = "idle", !.holds = [s \in Sources |-> 0]]
AdmitTerminal ==
    /\ st.terminalPending # 0 /\ OutputRoom
    /\ st' = [st EXCEPT !.output = Append(@, st.terminalPending),
       !.admittedOutput = Append(@, st.terminalPending), !.terminalPending = 0]
Close ==
    /\ st.phase = "Retiring" /\ st.terminalChosen /\ st.terminalPending = 0
    /\ st.writerFailed \/ (st.output = <<>> /\ st.writerStage = "idle")
    /\ ~st.writerPending /\ ~PendingFailure
    /\ st' = [st EXCEPT !.phase = "Closed"]

Next ==
    \/ \E source \in Participants : Recognize(source) \/ PublishRecognized(source)
          \/ HoldProducer(source) \/ PublishBarrierAck(source) \/ ReceiveBarrierAck(source)
    \/ \E lane \in {"critical", "bulk"} : Drain(lane)
    \/ PublishBulk \/ BeginBarrier \/ ReleaseBarrier \/ ReadyCandidate
    \/ \E kind \in {"ready", "retry"} : Success(kind)
    \/ \E g \in Generations : PublishCommand(g) \/ ReplayCommand(g) \/ CancelCommand(g) \/ StartExecution(g)
          \/ RejectSpawnOutput(g) \/ Acquire(g) \/ Quiesce(g) \/ StopWaiting(g)
          \/ PublishOffer(g) \/ LoseTransferAck(g) \/ ReceiveTransferAck(g)
          \/ DuplicateTransferAck(g) \/ RequestStatus(g) \/ ReceiveStatus(g)
          \/ ClaimCleanup(g) \/ BeliefCleanup(g) \/ SettleTicket(g)
          \/ ObserveQuiescence(g) \/ ObserveCleanup(g)
    \/ \E g \in Generations, failed \in BOOLEAN : FinishCleanup(g, failed)
    \/ TakeCommand \/ ReceiveOffer \/ StaleTransferAck \/ RetireAttempt \/ RetryProbe
    \/ OperationalFailure \/ ReadyOutputFailure
    \/ BeginWrite \/ PartialWrite \/ CompleteWrite \/ WriterFailure \/ HandleWriterFailure
    \/ CommitTerminal \/ AdmitTerminal \/ Close

Spec == Init /\ [][Next]_vars
AdmissionFairness ==
    /\ \A source \in Participants : WF_vars(PublishRecognized(source))
        /\ WF_vars(HoldProducer(source)) /\ WF_vars(PublishBarrierAck(source))
        /\ WF_vars(ReceiveBarrierAck(source))
    /\ \A lane \in {"critical", "bulk"} : WF_vars(Drain(lane))
    /\ WF_vars(ReleaseBarrier)
    /\ WF_vars(HandleWriterFailure)
WorkerFairness ==
    /\ WF_vars(TakeCommand) /\ WF_vars(ReceiveOffer)
    /\ \A g \in Generations : WF_vars(CancelCommand(g))
        /\ WF_vars(Acquire(g) \/ Quiesce(g)) /\ WF_vars(PublishOffer(g))
        /\ WF_vars(RequestStatus(g)) /\ WF_vars(ReceiveStatus(g))
        /\ WF_vars(ClaimCleanup(g)) /\ WF_vars(SettleTicket(g))
        /\ WF_vars(ObserveQuiescence(g)) /\ WF_vars(ObserveCleanup(g))
        /\ WF_vars(\E failed \in BOOLEAN : FinishCleanup(g, failed))
WriterFairness == WF_vars(BeginWrite) /\ WF_vars(CompleteWrite \/ WriterFailure)
CoordinatorFairness == WF_vars(CommitTerminal) /\ WF_vars(AdmitTerminal) /\ WF_vars(Close)
FairSpec == Spec /\ AdmissionFairness /\ WorkerFairness /\ WriterFairness /\ CoordinatorFairness
NoWorkerFairSpec == Spec /\ AdmissionFairness /\ WriterFairness /\ CoordinatorFairness
NoWriterFairSpec == Spec /\ AdmissionFairness /\ WorkerFairness /\ CoordinatorFairness
NoAdmissionFairSpec == Spec /\ WorkerFairness /\ WriterFairness /\ CoordinatorFairness
TerminationCloses == (st.intent # None) ~> (st.phase = "Closed")
BarrierProgress == st.barrier = "requested" ~> (st.barrier # "requested" \/ BarrierComplete)

TypeOK ==
    /\ st.phase \in {"Created", "Starting", "Active", "Retiring", "Closed"}
    /\ st.generation \in 0..MaxGeneration
    /\ \A source \in Sources : st.handled[source] <= st.admitted[source]
          /\ st.admitted[source] <= st.recognized[source]
          /\ st.recognized[source] <= Len(Facts(source))
    /\ \A lane \in {"critical", "bulk"} : Len(st.mailboxes[lane]) <= Capacity
    /\ Len(st.commands) <= Capacity /\ Len(st.offers) <= Capacity /\ Len(st.output) <= Capacity
NoObservedReadyOvertake == ~st.audit.readyOvertake
NoObservedRetryOvertake == ~st.audit.retryOvertake
NoPrematureBarrierAck == ~st.audit.ackGap
NoBarrierCycle == st.barrier = "requested" /\
    (\E lane \in {"critical", "bulk"} : Len(st.mailboxes[lane]) > 0) => CanDrain
ResourceOwned == \A g \in Generations : st.resource[g] => st.owner[g] \in {"producer", "supervisor"}
ExclusiveCleanup == ~st.audit.twoCleaners /\
    \A g \in Generations : st.stage[g] = "cleaning" => st.cleanupActor[g] = st.owner[g]
NoStaleTransfer == ~st.audit.staleTransfer
RetrySettled == ~st.audit.earlyRetry
NoFalseSettlement == \A g \in Generations : st.stage[g] = "settled" => TicketSettled(g)
NoInvalidExecution == ~st.audit.invalidExecution
SpawnHasAdmission == ~st.audit.spawnWithoutAdmission
TerminalIndices == {i \in 1..Len(st.journal) : st.journal[i].kind \in TerminalKinds}
OneTerminalOutput == Cardinality(TerminalIndices) <= 1
NoOutputAfterTerminal == \A i \in TerminalIndices : i = Len(st.journal)
OrderedOutput ==
    /\ \A i \in 1..Len(st.admittedOutput) : st.admittedOutput[i] = i
    /\ \A i \in 1..Len(st.delivered) : st.delivered[i] = i
PrimaryPreserved == ~st.audit.primaryReplaced
AllErrorsRetained == \A i \in 1..Len(st.requiredErrors) :
    st.requiredErrors[i] = st.primary \/ st.requiredErrors[i] \in {st.diagnostics[j] : j \in 1..Len(st.diagnostics)}
NoDuplicatePrimary == st.primary \notin {st.diagnostics[j] : j \in 1..Len(st.diagnostics)}
WriterDiagnosticOwned == st.writerFailed => st.writerPending \/ st.writerFault \in
    {st.primary} \cup {st.diagnostics[j] : j \in 1..Len(st.diagnostics)}
ResidualOwned == \A g \in Generations : st.residual[g] =>
    st.resource[g] /\ st.owner[g] = "supervisor" /\ Error("cleanup", g) \in
    {st.primary} \cup {st.diagnostics[j] : j \in 1..Len(st.diagnostics)}
ClosedSettled == st.phase = "Closed" => OldSettled
TerminalMonotonic == [][st.intent # None => st'.intent = st.intent]_vars

\* Coverage targets: their NEGATIONS are deliberately checked to get traces.
NoBackpressureWitness == ~(st.intent = "STOP" /\ st.audit.blockedStop)
NoLostAckWitness == ~(st.phase = "Closed" /\ st.ackLost[1]
                      /\ st.intent = "STOP" /\ st.audit.ackLostBeforeTerminal
                      /\ ~st.resource[1]
                      /\ st.cleanupActor[1] = "supervisor")
NoLateAcquireWitness == ~(st.phase = "Closed" /\ st.audit.lateAcquired
                         /\ st.intent = "STOP"
                         /\ ~st.resource[1]
                         /\ st.cleanupActor[1] = "producer")
NoRetryWaitWitness == ~(st.audit.retryRejected /\ ~st.quiet[st.generation])
NoRevocationWitness == ~(st.intent = "STOP" /\ st.stage[1] = "queued")
NoSpawnRejectWitness == ~st.audit.spawnEnqueueRejected
NoWriterAfterSpawnWitness == ~(st.writerFailed /\ st.stage[1] = "running"
    /\ \E i \in 1..Len(st.journal) : st.journal[i].kind = "PROCESS_STARTING")
NoTerminalWriterWitness == ~(st.audit.terminalWriterFailed /\ st.primary.code = "operational"
    /\ st.stage[1] = "settled" /\ st.cleanupActor[1] # None)
NoDelayedAckWitness == ~(st.intent = "STOP" /\ st.audit.ackAfterTerminal)
NoDuplicateAckWitness == ~st.ackDuplicate[1]
NoStaleAckWitness == ~st.staleAckSeen
=============================================================================
