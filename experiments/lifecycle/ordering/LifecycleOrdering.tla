-------------------------- MODULE LifecycleOrdering --------------------------
\* SPDX-License-Identifier: Apache-2.0
EXTENDS Naturals, Sequences, FiniteSets, TLC

CONSTANTS Design, EffectPolicy, RetrySettlement, TailFacts, Focus, MaxGeneration,
          InterruptEnabled, ResourceNames

\* Designs: dispatch, atomic, fence; fence mutants: ack-only, early-ack,
\* no-drain. Effect policies: validated, irrevocable, generation-only.
Sources == {"control", "interrupt"}
Kinds == {"ready", "retry"}
Generations == 1..MaxGeneration
Effects == Generations \X ResourceNames
TerminalFacts == {"STOP", "EOF", "BAD", "SIGNAL", "OBSERVER_FAILURE"}
None == "none"
Phases == {"Created", "Starting", "Active", "Retiring", "Closed"}
Stages == {"absent", "authorized", "running", "offered", "adopted",
           "cleaning", "settled", "cancelled"}
Owners == {None, "effect", "session"}
Entry(kind, g) == [kind |-> kind, generation |-> g]
SpawnTickets(g) == IF "aux" \in ResourceNames
                     THEN <<<<g, "child">>, <<g, "aux">>>> ELSE <<<<g, "child">>>>
Diagnostic(e, kind) ==
    [ticket |-> e, code |-> kind,
     nested |-> IF kind = "nested" THEN <<"stream-close-detail">> ELSE <<>>]
Failure(kind, g) == Diagnostic(<<g, "child">>, kind)
NoFailure == Failure(None, 0)

VARIABLES phase, generation, intent, primary, diagnostics, candidate, retry,
          cleanupStarted, recognized, admitted, handled, control, interrupt,
          barrier, barrierTarget, stage, owner, effectOutbox, protocol,
          terminalSnapshot, audit

vars == <<phase, generation, intent, primary, diagnostics, candidate, retry,
          cleanupStarted, recognized, admitted, handled, control, interrupt,
          barrier, barrierTarget, stage, owner, effectOutbox, protocol,
          terminalSnapshot, audit>>

Facts(s) == IF s = "control" THEN control ELSE <<interrupt>>
CurrentEffects == {e \in Effects : e[1] = generation}
Settled(g) == \A e \in Effects : e[1] = g => stage[e] \in {"settled", "cancelled"}
NoPendingAdmission == recognized = admitted
NoPendingDispatch == admitted = handled
ObservedTerminal ==
    \E s \in Sources : \E i \in 1..recognized[s] : Facts(s)[i] \in TerminalFacts
AdmittedTerminal ==
    \E s \in Sources : \E i \in 1..admitted[s] : Facts(s)[i] \in TerminalFacts
PendingFailures ==
    \E s \in Sources : \E i \in 1..recognized[s] :
        i > handled[s] /\ Facts(s)[i] \in {"BAD", "OBSERVER_FAILURE"}
FenceDesign == Design \in {"fence", "ack-only", "early-ack", "no-drain"}
ValidEffect(e) ==
    /\ e[1] = generation /\ phase = "Starting" /\ intent = None
Eligibility(kind, token) ==
    /\ token = generation /\ intent = None
    /\ IF kind = "ready"
          THEN /\ phase = "Starting" /\ candidate = token
               /\ stage[<<token, "child">>] = "adopted"
          ELSE /\ phase = "Retiring" /\ retry /\ cleanupStarted
               /\ generation < MaxGeneration
               /\ (RetrySettlement = "early" \/ Settled(generation))
AdmissionBarrier(kind, token) ==
    /\ (Design = "no-drain" \/ NoPendingDispatch)
    /\ IF FenceDesign
          THEN /\ barrier = "acked"
               /\ barrierTarget = Entry(kind, token)
          ELSE TRUE
SuccessAllowed(kind, token) == Eligibility(kind, token) /\ AdmissionBarrier(kind, token)

Init ==
    /\ phase = "Created" /\ generation = 0 /\ intent = None
    /\ primary = NoFailure /\ diagnostics = <<>> /\ candidate = 0 /\ retry = FALSE
    /\ cleanupStarted = FALSE
    /\ recognized = [s \in Sources |-> 0]
    /\ admitted = [s \in Sources |-> 0]
    /\ handled = [s \in Sources |-> 0]
    /\ control \in {<<"START", tail>> : tail \in TailFacts}
    /\ interrupt \in {"SIGNAL", "OBSERVER_FAILURE"}
    /\ barrier = "idle" /\ barrierTarget = Entry(None, 0)
    /\ stage = [e \in Effects |-> "absent"]
    /\ owner = [e \in Effects |-> None]
    /\ effectOutbox = <<>> /\ protocol = <<Entry("SESSION_CREATED", 0)>>
    /\ terminalSnapshot = [primary |-> NoFailure, diagnostics |-> <<>>]
    /\ audit = [readyOvertake |-> FALSE, retryOvertake |-> FALSE,
                 readyAfterTerminal |-> FALSE, retryAfterTerminal |-> FALSE,
                 staleSuccess |-> FALSE, forbiddenAuthorization |-> FALSE,
                 invalidExecution |-> FALSE, staleAdoption |-> FALSE,
                 lateDispatchRejected |-> FALSE, lateCompletionRejected |-> FALSE,
                 separateStart |-> FALSE, batchCommit |-> FALSE,
                 unsettledRetry |-> FALSE, primaryReplaced |-> FALSE,
                 admittedCleanup |-> <<>>]

\* Recognition, not byte arrival: one complete frame or observer failure.
\* The original immutable fact is producer-owned until admission; no copied
\* pending-control payload is used by the success decision.
Recognize(s, count) ==
    /\ phase # "Closed" /\ count \in 1..2
    /\ (s = "control" \/ InterruptEnabled)
    /\ recognized[s] + count <= Len(Facts(s))
    \* Restrict only the deliberately broken dispatch witnesses to the requested
    \* history. Good designs explore all recognition orderings and batches.
    /\ (Design # "dispatch" \/ Focus = "all" \/ s # "control"
         \/ recognized[s] # 1 \/
         IF Focus = "ready" THEN generation > 0
         ELSE phase = "Retiring" /\ retry /\ Settled(generation))
    /\ (Design # "dispatch" \/ Focus = "all" \/ count = 1)
    /\ (Design \notin {"fence", "early-ack", "no-drain"} \/ barrier # "acked")
    /\ recognized' = [recognized EXCEPT ![s] = @ + count]
    /\ admitted' = IF Design = "atomic"
                     THEN [admitted EXCEPT ![s] = recognized[s] + count]
                     ELSE admitted
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, handled, control, interrupt, barrier,
                   barrierTarget, stage, owner, effectOutbox, protocol,
                   terminalSnapshot, audit>>

\* The minimum post-ack contract forbids withholding, not new admission.
\* A producer may recognize after acknowledgement if admission is atomic.
DirectAdmission(s, count) ==
    /\ FenceDesign /\ barrier = "acked" /\ phase # "Closed"
    /\ (s = "control" \/ InterruptEnabled) /\ count \in 1..2
    /\ recognized[s] + count <= Len(Facts(s))
    /\ recognized' = [recognized EXCEPT ![s] = @ + count]
    /\ admitted' = [admitted EXCEPT ![s] = recognized[s] + count]
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, handled, control, interrupt, barrier,
                   barrierTarget, stage, owner, effectOutbox, protocol,
                   terminalSnapshot, audit>>

Admit(s) ==
    /\ phase # "Closed" /\ admitted[s] < recognized[s]
    /\ admitted' = [admitted EXCEPT ![s] = @ + 1]
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, handled, control, interrupt,
                   barrier, barrierTarget, stage, owner, effectOutbox, protocol,
                   terminalSnapshot, audit>>

\* Authority dispatch preserves each source's frame order. Across sources,
\* first committed terminal intent/primary wins, rather than wall-clock order.
Handle(s) ==
    /\ phase # "Closed" /\ handled[s] < admitted[s]
    /\ LET fact == Facts(s)[handled[s] + 1]
           starting == fact = "START" /\ phase = "Created" /\ intent = None
           duplicate == fact = "START" /\ phase # "Created" /\ intent = None
           ending == (fact \in TerminalFacts \/ duplicate) /\ intent = None
           failed == fact \in {"BAD", "OBSERVER_FAILURE"} \/ duplicate
           retained == fact \in {"BAD", "OBSERVER_FAILURE"} /\ intent # None
           error == Failure(IF fact = "OBSERVER_FAILURE" THEN "observer" ELSE "protocol", generation)
       IN /\ handled' = [handled EXCEPT ![s] = @ + 1]
          /\ generation' = IF starting THEN 1 ELSE generation
          /\ phase' = IF ending THEN "Retiring"
                        ELSE IF starting THEN "Starting" ELSE phase
          /\ intent' = IF ending THEN IF failed THEN "failure" ELSE fact ELSE intent
          /\ primary' = IF ending /\ failed
                           THEN Failure(IF fact = "OBSERVER_FAILURE" THEN "observer" ELSE "protocol", generation)
                           ELSE IF retained /\ primary = NoFailure THEN error ELSE primary
          /\ diagnostics' = IF retained /\ primary # NoFailure
                               THEN Append(diagnostics, error) ELSE diagnostics
          /\ candidate' = IF ending THEN 0 ELSE candidate
          /\ retry' = IF starting \/ ending THEN FALSE ELSE retry
          /\ cleanupStarted' = IF starting THEN FALSE ELSE cleanupStarted
          /\ stage' = IF starting
                         THEN [e \in Effects |-> IF e[1] = 1 THEN "authorized" ELSE stage[e]]
                         ELSE stage
          /\ owner' = IF starting
                         THEN [e \in Effects |-> IF e[1] = 1 THEN "effect" ELSE owner[e]]
                         ELSE owner
          /\ effectOutbox' = IF starting
                                THEN effectOutbox \o SpawnTickets(1)
                                ELSE effectOutbox
          /\ protocol' = protocol
          /\ audit' = [audit EXCEPT !.separateStart = @ \/ starting]
    /\ UNCHANGED <<recognized, admitted, control, interrupt,
                   barrier, barrierTarget, terminalSnapshot>>

\* Optional atomic authority handling of an admitted START+terminal batch.
\* START's authorization is recorded, but is invalid in this final snapshot.
HandleBatch ==
    /\ phase = "Created" /\ intent = None
    /\ handled["control"] = 0 /\ admitted["control"] = 2
    /\ handled' = [handled EXCEPT !["control"] = 2]
    /\ generation' = 1 /\ phase' = "Retiring"
    /\ intent' = IF control[2] = "BAD" THEN "failure" ELSE control[2]
    /\ primary' = IF control[2] = "BAD" THEN Failure("protocol", 1) ELSE NoFailure
    /\ stage' = [e \in Effects |-> IF e[1] = 1 THEN "authorized" ELSE stage[e]]
    /\ owner' = [e \in Effects |-> IF e[1] = 1 THEN "effect" ELSE owner[e]]
    /\ effectOutbox' = effectOutbox \o SpawnTickets(1)
    /\ protocol' = protocol
    /\ audit' = [audit EXCEPT !.batchCommit = TRUE]
    /\ UNCHANGED <<candidate, retry, cleanupStarted, diagnostics, recognized,
                   admitted, control, interrupt, barrier, barrierTarget,
                   terminalSnapshot>>

ReadyCandidate(token) ==
    /\ token \in Generations /\ token = generation
    /\ phase = "Starting" /\ intent = None /\ candidate # token
    /\ (Design # "dispatch" \/ Focus # "ready" \/ recognized["control"] = 2)
    /\ candidate' = token
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, retry,
                   cleanupStarted, recognized, admitted, handled, control,
                   interrupt, barrier, barrierTarget, stage, owner, effectOutbox,
                   protocol, terminalSnapshot, audit>>

AttemptExit(token, kind) ==
    /\ token \in Generations /\ token = generation
    /\ kind \in {"retryable", "operational", "natural"}
    /\ intent = None /\ phase \in {"Starting", "Active"}
    /\ stage[<<token, "child">>] \in {"offered", "adopted"}
    /\ phase' = "Retiring" /\ candidate' = 0
    /\ retry' = (phase = "Starting" /\ kind = "retryable" /\ generation < MaxGeneration)
    /\ intent' = IF retry' THEN None ELSE IF phase = "Active" THEN "process_exit" ELSE "failure"
    /\ primary' = IF intent' = "failure" THEN Failure("operational", generation) ELSE primary
    /\ UNCHANGED <<generation, diagnostics, cleanupStarted, recognized, admitted,
                   handled, control, interrupt, barrier, barrierTarget, stage,
                   owner, effectOutbox, protocol, terminalSnapshot, audit>>

RequestBarrier(kind, token) ==
    /\ FenceDesign /\ barrier = "idle" /\ Eligibility(kind, token)
    /\ barrier' = "requested" /\ barrierTarget' = Entry(kind, token)
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, admitted, handled, control,
                   interrupt, stage, owner, effectOutbox, protocol, terminalSnapshot, audit>>

Acknowledge ==
    /\ barrier = "requested"
    /\ (Design = "early-ack" \/ NoPendingAdmission)
    /\ barrier' = "acked"
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, admitted, handled, control,
                   interrupt, barrierTarget, stage, owner, effectOutbox, protocol,
                   terminalSnapshot, audit>>

ReleaseBarrier ==
    /\ barrier # "idle"
    /\ (intent # None \/ ~Eligibility(barrierTarget.kind, barrierTarget.generation))
    /\ barrier' = "idle" /\ barrierTarget' = Entry(None, 0)
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, admitted, handled, control,
                   interrupt, stage, owner, effectOutbox, protocol, terminalSnapshot, audit>>

\* The same rule guards READY and retry. No action tests a shadow STOP value.
Success(kind, token) ==
    /\ kind \in Kinds /\ token \in Generations
    /\ (Focus = "all" \/ Focus = kind) /\ SuccessAllowed(kind, token)
    /\ LET next == IF kind = "retry" THEN generation + 1 ELSE generation
       IN /\ phase' = IF kind = "ready" THEN "Active" ELSE "Starting"
          /\ generation' = next /\ candidate' = 0 /\ retry' = FALSE
          /\ cleanupStarted' = FALSE
          /\ stage' = IF kind = "retry"
                         THEN [e \in Effects |-> IF e[1] = next THEN "authorized" ELSE stage[e]]
                         ELSE stage
          /\ owner' = IF kind = "retry"
                         THEN [e \in Effects |-> IF e[1] = next THEN "effect" ELSE owner[e]]
                         ELSE owner
          /\ effectOutbox' = IF kind = "retry"
                                THEN effectOutbox \o SpawnTickets(next)
                                ELSE effectOutbox
          /\ protocol' = IF kind = "ready"
                            THEN Append(protocol, Entry("PROCESS_READY", next)) ELSE protocol
          /\ audit' = [audit EXCEPT
                 !.readyOvertake = @ \/ (kind = "ready" /\ ObservedTerminal),
                 !.retryOvertake = @ \/ (kind = "retry" /\ ObservedTerminal),
                 !.readyAfterTerminal = @ \/ (kind = "ready" /\ intent # None),
                 !.retryAfterTerminal = @ \/ (kind = "retry" /\ intent # None),
                 !.staleSuccess = @ \/ token # generation,
                 !.forbiddenAuthorization = @ \/ (kind = "retry" /\ intent # None),
                 !.unsettledRetry = @ \/ (kind = "retry" /\ ~Settled(generation))]
    /\ barrier' = "idle" /\ barrierTarget' = Entry(None, 0)
    /\ UNCHANGED <<intent, primary, diagnostics, recognized, admitted, handled,
                   control, interrupt, terminalSnapshot>>

\* Execution begins here, at the same atomic point as validity checking.
\* A generation alone cannot invalidate current-generation work after STOP.
DispatchEffect(e) ==
    /\ e \in Effects /\ stage[e] = "authorized" /\ phase # "Closed"
    /\ CASE EffectPolicy = "validated" -> ValidEffect(e)
         [] EffectPolicy = "generation-only" -> e[1] = generation
         [] EffectPolicy = "irrevocable" -> TRUE
    /\ stage' = [stage EXCEPT ![e] = "running"]
    /\ audit' = [audit EXCEPT !.invalidExecution = @ \/ ~ValidEffect(e)]
    \* Protocol v1 announces an actual spawn attempt, not a proposal. Pair the
    \* irreversible PROCESS_STARTING entry with execution commitment.
    /\ protocol' = IF e[2] = "child" THEN Append(protocol, Entry("PROCESS_STARTING", e[1]))
                                         ELSE protocol
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, admitted, handled, control,
                   interrupt, barrier, barrierTarget, owner, effectOutbox,
                   terminalSnapshot>>

CancelEffect(e) ==
    /\ e \in Effects /\ stage[e] = "authorized" /\ ~ValidEffect(e)
    /\ stage' = [stage EXCEPT ![e] = "cancelled"]
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, admitted, handled, control,
                   interrupt, barrier, barrierTarget, owner, effectOutbox,
                   protocol, terminalSnapshot, audit>>

CompleteEffect(e, succeeded) ==
    /\ e \in Effects /\ stage[e] = "running" /\ succeeded \in BOOLEAN
    /\ stage' = [stage EXCEPT ![e] = IF succeeded THEN "offered" ELSE "settled"]
    /\ LET failedCurrent == ~succeeded /\ ValidEffect(e)
       IN /\ phase' = IF failedCurrent THEN "Retiring" ELSE phase
          /\ intent' = IF failedCurrent THEN "failure" ELSE intent
          /\ primary' = IF failedCurrent THEN Failure("operational", generation) ELSE primary
          /\ candidate' = IF failedCurrent THEN 0 ELSE candidate
    /\ UNCHANGED <<generation, diagnostics, retry, cleanupStarted, recognized,
                   admitted, handled, control, interrupt, barrier, barrierTarget,
                   owner, effectOutbox, protocol, terminalSnapshot, audit>>

AdoptEffect(e) ==
    /\ e \in Effects /\ stage[e] = "offered" /\ ValidEffect(e)
    /\ stage' = [stage EXCEPT ![e] = "adopted"]
    /\ owner' = [owner EXCEPT ![e] = "session"]
    /\ audit' = [audit EXCEPT !.staleAdoption = @ \/ e[1] # generation \/ intent # None]
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, admitted, handled, control,
                   interrupt, barrier, barrierTarget, effectOutbox, protocol, terminalSnapshot>>

BeginCleanup ==
    /\ phase = "Retiring" /\ ~cleanupStarted
    /\ cleanupStarted' = TRUE
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, recognized, admitted, handled, control, interrupt,
                   barrier, barrierTarget, stage, owner, effectOutbox, protocol,
                   terminalSnapshot, audit>>

DispatchCleanup(e) ==
    /\ e \in Effects /\ cleanupStarted
    /\ stage[e] \in {"offered", "adopted"}
    /\ (phase = "Retiring" \/ e[1] # generation)
    /\ stage' = [stage EXCEPT ![e] = "cleaning"]
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, admitted, handled, control,
                   interrupt, barrier, barrierTarget, owner, effectOutbox,
                   protocol, terminalSnapshot, audit>>

\* A completed cleanup/timeout fact is admitted here. Diagnostic trees stay
\* intact. The first failure becomes primary only when no primary exists;
\* diagnostics contain secondary failures, not a duplicate of that primary.
FinishCleanup(e, kind) ==
    /\ e \in Effects /\ stage[e] = "cleaning"
    /\ kind \in {"ok", "nested", "extra"}
    /\ LET error == Diagnostic(e, kind)
           failed == kind # "ok"
       IN /\ stage' = [stage EXCEPT ![e] = "settled"]
          /\ primary' = IF failed /\ primary = NoFailure THEN error ELSE primary
          /\ diagnostics' = IF failed /\ primary # NoFailure
                               THEN Append(diagnostics, error) ELSE diagnostics
          /\ intent' = IF failed /\ intent = None THEN "failure" ELSE intent
          /\ audit' = [audit EXCEPT
                 !.primaryReplaced = @ \/ (primary # NoFailure /\ primary' # primary),
                 !.admittedCleanup = IF failed THEN Append(@, error) ELSE @]
    /\ UNCHANGED <<phase, generation, candidate, retry, cleanupStarted, recognized,
                   admitted, handled, control, interrupt, barrier, barrierTarget,
                   owner, effectOutbox, protocol, terminalSnapshot>>

Close ==
    /\ phase = "Retiring" /\ intent # None /\ cleanupStarted
    /\ \A e \in Effects : stage[e] \in {"absent", "settled", "cancelled"}
    \* A guarded observer failure recognized before close remains owned even
    \* if normal publication is withheld. Account for it before reporting.
    /\ ~PendingFailures
    /\ phase' = "Closed"
    /\ protocol' = IF primary # NoFailure THEN Append(protocol, Entry("ERROR", generation))
                    ELSE IF intent \in {"STOP", "process_exit"}
                            THEN Append(protocol, Entry("SESSION_CLOSED", generation))
                            ELSE protocol
    /\ terminalSnapshot' = [primary |-> primary, diagnostics |-> diagnostics]
    /\ UNCHANGED <<generation, intent, primary, diagnostics, candidate, retry,
                   cleanupStarted, recognized, admitted, handled, control,
                   interrupt, barrier, barrierTarget, stage, owner, effectOutbox, audit>>

Respond(e) == \E succeeded \in BOOLEAN : CompleteEffect(e, succeeded)
Dispose(e) == \E kind \in {"ok", "nested", "extra"} : FinishCleanup(e, kind)
RejectLate(e, kind) ==
    /\ e \in Effects /\ e[1] < generation /\ kind \in {"dispatch", "completion"}
    /\ stage[e] \in {"settled", "cancelled"}
    /\ IF kind = "dispatch" THEN ~audit.lateDispatchRejected ELSE ~audit.lateCompletionRejected
    /\ audit' = [audit EXCEPT
          !.lateDispatchRejected = @ \/ kind = "dispatch",
          !.lateCompletionRejected = @ \/ kind = "completion"]
    /\ UNCHANGED <<phase, generation, intent, primary, diagnostics, candidate,
                   retry, cleanupStarted, recognized, admitted, handled, control,
                   interrupt, barrier, barrierTarget, stage, owner, effectOutbox,
                   protocol, terminalSnapshot>>
Next ==
    \/ \E s \in Sources, count \in 1..2 : Recognize(s, count)
    \/ \E s \in Sources, count \in 1..2 : DirectAdmission(s, count)
    \/ \E s \in Sources : Admit(s) \/ Handle(s)
    \/ HandleBatch
    \/ \E token \in Generations : ReadyCandidate(token)
    \/ \E token \in Generations, kind \in {"retryable", "operational", "natural"} : AttemptExit(token, kind)
    \/ \E kind \in Kinds, token \in Generations : RequestBarrier(kind, token) \/ Success(kind, token)
    \/ Acknowledge \/ ReleaseBarrier
    \/ \E e \in Effects : DispatchEffect(e) \/ CancelEffect(e) \/ Respond(e)
                          \/ AdoptEffect(e) \/ DispatchCleanup(e) \/ Dispose(e)
    \/ BeginCleanup \/ Close
    \/ \E e \in Effects, kind \in {"dispatch", "completion"} : RejectLate(e, kind)

Spec == Init /\ [][Next]_vars
FairSpec == Spec /\ WF_vars(BeginCleanup) /\ WF_vars(Close)
            /\ \A s \in Sources : WF_vars(Admit(s)) /\ WF_vars(Handle(s))
            /\ \A e \in Effects : WF_vars(CancelEffect(e)) /\ WF_vars(Respond(e))
                                  /\ WF_vars(DispatchCleanup(e)) /\ WF_vars(Dispose(e))
CoordinatorFairSpec == Spec /\ WF_vars(BeginCleanup) /\ WF_vars(Close)
TerminationCloses == (intent # None) ~> (phase = "Closed")

TypeOK ==
    /\ phase \in Phases /\ generation \in 0..MaxGeneration
    /\ intent \in TerminalFacts \cup {None, "failure", "process_exit"}
    /\ candidate \in 0..MaxGeneration /\ retry \in BOOLEAN /\ cleanupStarted \in BOOLEAN
    /\ recognized \in [Sources -> 0..2] /\ admitted \in [Sources -> 0..2]
    /\ handled \in [Sources -> 0..2]
    /\ \A s \in Sources : handled[s] <= admitted[s] /\ admitted[s] <= recognized[s]
                          /\ recognized[s] <= Len(Facts(s))
    /\ stage \in [Effects -> Stages] /\ owner \in [Effects -> Owners]
    /\ barrier \in {"idle", "requested", "acked"}
NoReadyAfterTerminal == ~audit.readyAfterTerminal
NoRetryAfterTerminal == ~audit.retryAfterTerminal
NoObservedReadyOvertake == ~audit.readyOvertake
NoObservedRetryOvertake == ~audit.retryOvertake
NoStaleSuccess == ~audit.staleSuccess
ResourceOwned == \A e \in Effects : stage[e] # "absent" => owner[e] # None
NoStaleAdoption == ~audit.staleAdoption
TerminalIndices == {i \in 1..Len(protocol) : protocol[i].kind \in {"ERROR", "SESSION_CLOSED"}}
OneTerminalOutput == Cardinality(TerminalIndices) <= 1
NoOutputAfterTerminal == \A i \in TerminalIndices : i = Len(protocol)
PrimaryPreserved == ~audit.primaryReplaced
DiagnosticsRetained ==
    \A i \in 1..Len(audit.admittedCleanup) :
        audit.admittedCleanup[i] = primary \/
        \E j \in 1..Len(diagnostics) : audit.admittedCleanup[i] = diagnostics[j]
NoDuplicatePrimaryDiagnostic == \A i \in 1..Len(diagnostics) : diagnostics[i] # primary
RecognizedFailuresRetained ==
    LET codes == {primary.code} \cup {diagnostics[j].code : j \in 1..Len(diagnostics)}
    IN \A s \in Sources : \A i \in 1..handled[s] :
        /\ (Facts(s)[i] = "BAD" => "protocol" \in codes)
        /\ (Facts(s)[i] = "OBSERVER_FAILURE" => "observer" \in codes)
NoForbiddenAuthorization == ~audit.forbiddenAuthorization
NoInvalidExecution == ~audit.invalidExecution
RetryAfterSettlement == ~audit.unsettledRetry
ClosedSettled == phase = "Closed" => \A e \in Effects : stage[e] \in {"absent", "settled", "cancelled"}
TerminalIntentMonotonic == [][intent # None => intent' = intent]_vars

\* Witnesses are checked as negated invariants to produce concrete reachable
\* histories. They are not extra safety obligations of the good designs.
NoBatchWitness == ~(phase = "Retiring" /\ audit.batchCommit /\ handled["control"] = 2 /\ generation = 1
                     /\ stage[<<1, "child">>] = "authorized" /\ intent = "STOP")
NoPendingEffectWitness == ~(intent = "STOP" /\ stage[<<1, "child">>] = "authorized"
                             /\ handled["control"] = 2 /\ audit.separateStart)
NoLateCompletionWitness == ~(intent = "STOP" /\ stage[<<1, "child">>] = "offered")
NoDiagnosticsWitness == ~(phase = "Closed" /\ primary.code = "operational"
    /\ Len(diagnostics) = 2 /\ diagnostics[1].code = "nested" /\ diagnostics[2].code = "extra")
NoStaleRejectionWitness == ~(generation = 2 /\ audit.lateDispatchRejected
                             /\ audit.lateCompletionRejected)

=============================================================================
