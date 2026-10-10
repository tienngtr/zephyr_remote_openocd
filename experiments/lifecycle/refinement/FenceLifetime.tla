--------------------------- MODULE FenceLifetime -----------------------------
\* SPDX-License-Identifier: Apache-2.0
\* Focused completion of the admission contract: a cut belongs to one proposal.
\* Starts after START and a readiness candidate; the full boundary model checks
\* resource/queue/output obligations. No physical operation is performed here.
EXTENDS Naturals, Sequences, TLC
CONSTANT Mutant
VARIABLE st
vars == <<st>>

Init == st = [phase |-> "Starting", generation |-> 1, ready |-> TRUE,
    retry |-> FALSE, settled |-> TRUE, retired |-> FALSE, terminal |-> FALSE,
    recognized |-> 1, admitted |-> 1, handled |-> 1, mailbox |-> FALSE,
    epoch |-> 0, request |-> 0, kind |-> "none", requestGen |-> 0,
    hold |-> 0, ackBox |-> 0, receipt |-> 0, journal |-> <<>>,
    audit |-> [retarget |-> FALSE, stopOvertake |-> FALSE,
        fenceAborted |-> FALSE, stopDuringCleanup |-> FALSE]]

Eligible(kind) == ~st.terminal /\
    IF kind = "ready" THEN st.phase = "Starting" /\ st.ready
    ELSE st.phase = "Retiring" /\ st.retry /\ st.settled /\ st.generation = 1

RecognizeStop ==
    /\ st.recognized = 1 /\ st.hold = 0 /\ ~st.terminal
    /\ st' = [st EXCEPT !.recognized = 2,
        !.audit.stopDuringCleanup = st.phase = "Retiring" /\ ~st.settled]
PublishStop ==
    /\ st.recognized = 2 /\ st.admitted = 1 /\ ~st.mailbox
    /\ st' = [st EXCEPT !.admitted = 2, !.mailbox = TRUE]
DrainStop ==
    /\ st.mailbox
    /\ st' = [st EXCEPT !.handled = 2, !.mailbox = FALSE, !.terminal = TRUE,
        !.phase = "Terminating", !.request = 0, !.hold = 0,
        !.kind = "none", !.requestGen = 0]

Begin ==
    /\ st.request = 0 /\ st.epoch < 3
    /\ (Eligible("ready") \/ Eligible("retry"))
    /\ st' = [st EXCEPT !.epoch = @ + 1, !.request = st.epoch + 1,
        !.kind = IF Eligible("ready") THEN "ready" ELSE "retry",
        !.requestGen = st.generation]
Hold ==
    /\ st.request # 0 /\ st.hold = 0
    /\ st' = [st EXCEPT !.hold = st.request]
PublishAck ==
    /\ st.request # 0 /\ st.hold = st.request /\ st.ackBox = 0
    /\ st.receipt # st.request /\ st.recognized = st.admitted
    /\ st' = [st EXCEPT !.ackBox = st.request]
ReceiveAck ==
    /\ st.ackBox # 0
    /\ st' = [st EXCEPT !.receipt = IF st.request = st.ackBox THEN st.ackBox ELSE @,
        !.ackBox = 0]

ChildExit ==
    /\ st.phase = "Starting" /\ st.generation = 1 /\ ~st.retired /\ ~st.terminal
    /\ st' = [st EXCEPT !.phase = "Retiring", !.ready = FALSE,
        !.retry = TRUE, !.settled = FALSE, !.retired = TRUE,
        !.audit.fenceAborted = Mutant # "retarget" /\ st.request # 0 /\ st.hold # 0,
        !.request = IF Mutant = "retarget" THEN @ ELSE 0,
        !.hold = IF Mutant = "retarget" THEN @ ELSE 0,
        !.kind = IF Mutant = "retarget" THEN @ ELSE "none",
        !.requestGen = IF Mutant = "retarget" THEN @ ELSE 0]
Settle ==
    /\ st.retired /\ ~st.settled
    \* A final quiescence/disposal proof, not merely a stopped wait.
    /\ st' = [st EXCEPT !.settled = TRUE]

Commit(kind) ==
    /\ kind \in {"ready", "retry"} /\ Eligible(kind)
    /\ st.request # 0 /\ st.receipt = st.request /\ st.handled = st.admitted
    /\ (Mutant = "retarget" \/ (st.kind = kind /\ st.requestGen = st.generation))
    /\ st' = [st EXCEPT !.phase = IF kind = "ready" THEN "Active" ELSE "Starting",
        !.generation = IF kind = "retry" THEN @ + 1 ELSE @,
        !.ready = kind = "retry", !.retry = FALSE,
        !.journal = Append(@, [kind |-> kind,
            generation |-> IF kind = "retry" THEN st.generation + 1 ELSE st.generation]),
        !.audit.retarget = @ \/ st.kind # kind \/ st.requestGen # st.generation,
        !.audit.stopOvertake = @ \/ st.recognized = 2,
        !.request = 0, !.hold = 0, !.kind = "none", !.requestGen = 0]

Next == RecognizeStop \/ PublishStop \/ DrainStop \/ Begin \/ Hold
    \/ PublishAck \/ ReceiveAck \/ ChildExit \/ Settle
    \/ \E kind \in {"ready", "retry"} : Commit(kind)
Spec == Init /\ [][Next]_vars
OtherFairness == WF_vars(PublishStop) /\ WF_vars(DrainStop) /\ WF_vars(Hold)
    /\ WF_vars(PublishAck) /\ WF_vars(ReceiveAck) /\ WF_vars(Settle)
DecisionFairness == \A kind \in {"ready", "retry"} : WF_vars(Commit(kind))
FairSpec == Spec /\ OtherFairness /\ DecisionFairness
NoDecisionFairSpec == Spec /\ OtherFairness

TypeOK == st.generation \in 1..2 /\ st.epoch \in 0..3
    /\ 1 <= st.handled /\ st.handled <= st.admitted /\ st.admitted <= st.recognized
    /\ st.recognized <= 2
NoStaleProposalCommit == ~st.audit.retarget
NoObservedStopSuccess == ~st.audit.stopOvertake
NoHoldDuringCleanup == st.phase = "Retiring" /\ ~st.settled =>
    st.request = 0 /\ st.hold = 0
FenceEnds == st.request # 0 ~> st.request = 0
NoRetireStopWitness == ~(st.audit.fenceAborted /\ st.audit.stopDuringCleanup)
=============================================================================
