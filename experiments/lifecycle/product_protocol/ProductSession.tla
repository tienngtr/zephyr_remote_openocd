--------------------------- MODULE ProductSession ---------------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC
CONSTANTS Candidate, Broken, MaxChunks, Mode, ExitCode
VARIABLE s
vars == <<s>>
Attempts == 1..2
Streams == {"stdout", "stderr"}
EmptyAttempt == [stage |-> "absent", live |-> FALSE, owner |-> "none"]
Init == s = [local |-> "opening", remote |-> "created", gen |-> 0,
    attempts |-> [g \in Attempts |-> EmptyAttempt],
    start |-> "available", loss |-> "none", transport |-> TRUE,
    readySeen |-> FALSE, forwarded |-> FALSE, client |-> FALSE,
    localFault |-> FALSE, localPrimary |-> "none", localEstablished |-> "none",
    localDiagnostics |-> <<>>, localChild |-> <<>>,
    evidence |-> FALSE, retry |-> FALSE, deadline |-> FALSE,
    primary |-> "none", established |-> "none", diagnostics |-> <<>>,
    cleanupErrors |-> 0, writerBroken |-> FALSE, signalUsed |-> FALSE,
    observerFailed |-> FALSE,
    cause |-> "none", childResult |-> <<>>, terminal |-> FALSE,
    terminalCount |-> 0, resultSource |-> "none", resultValue |-> <<>>,
    terminalPrimary |-> "none", terminalDiagnostics |-> <<>>,
    wire |-> <<>>, protocolLog |-> <<>>, endedReceived |-> FALSE, attempted |-> {},
    produced |-> [stream \in Streams |-> 0],
    delivered |-> [stream \in Streams |-> 0],
    bulk |-> [stream \in Streams |-> <<>>],
    badClient |-> FALSE, badAttempt |-> FALSE, badStale |-> FALSE,
    badRetry |-> FALSE, badOrdering |-> FALSE,
    lateReady |-> FALSE, staleSeen |-> FALSE, observedOvertake |-> FALSE,
    retryOvertake |-> FALSE, readyOvertake |-> FALSE, lateAcquired |-> FALSE]

Primary(st, failure) == IF st.primary = "none" THEN
    [st EXCEPT !.primary = failure, !.established = failure]
    ELSE [st EXCEPT !.diagnostics = Append(@, failure)]
LocalPrimary(st, failure) == IF st.localPrimary = "none" THEN
    [st EXCEPT !.localPrimary = failure, !.localEstablished = failure]
    ELSE [st EXCEPT !.localDiagnostics = Append(@, failure)]
End(st, cause) == [st EXCEPT !.remote = "terminating", !.retry = FALSE,
    !.cause = IF @ = "none" THEN cause ELSE @]
Cut == Candidate # "v1" \/ s.loss \notin {"recognized", "admitted"}
AllSettled == \A g \in Attempts : s.attempts[g].stage \in {"absent", "settled"}
AllDisposed == \A g \in Attempts : ~s.attempts[g].live

Start == /\ s.start = "available" /\ s.remote = "created"
    /\ s' = [s EXCEPT !.start = "handled", !.remote = "starting", !.gen = 1,
        !.attempts[1] = [stage |-> "authorized", live |-> FALSE, owner |-> "producer"]]
LocalCancel == /\ s.local \in {"opening", "active"}
    /\ s' = [s EXCEPT !.local = "cancelling",
        !.loss = IF @ = "none" THEN "available" ELSE @]
TransportLoss == /\ s.transport /\ s.local # "ended"
    /\ s' = [LocalPrimary(s, "transport") EXCEPT !.transport = FALSE, !.local = "cancelling",
        !.loss = IF @ = "none" THEN "available" ELSE @]
LocalFailure == /\ ~s.localFault /\ s.local \in {"opening", "active"}
    /\ s' = [LocalPrimary(s, IF s.client THEN "gdb"
        ELSE IF Mode = "server" THEN "required-forward" ELSE "local-operation") EXCEPT
        !.localFault = TRUE, !.local = "cancelling",
        !.loss = IF @ = "none" THEN "available" ELSE @]
RecognizeLoss == /\ s.loss = "available" /\ s.remote # "ended"
    /\ s' = [s EXCEPT !.loss = "recognized"]
AdmitLoss == /\ s.loss = "recognized" /\ s.remote # "ended"
    /\ s' = [s EXCEPT !.loss = "admitted"]
HandleLoss == /\ s.loss = "admitted" /\ s.remote # "ended"
    /\ s' = [End(s, "controller-ended") EXCEPT !.loss = "handled"]

Dispatch == /\ s.gen > 0 /\ s.attempts[s.gen].stage = "authorized"
    /\ (s.remote = "starting" \/ Broken = "terminal-attempt")
    /\ ~s.writerBroken /\ Len(s.wire) < 2
    /\ s' = [s EXCEPT !.attempts[s.gen].stage = "producing",
        !.attempted = @ \cup {s.gen}, !.wire = Append(@, "ATTEMPT"),
        !.protocolLog = Append(@, "ATTEMPT"),
        !.badAttempt = s.remote # "starting"]
Revoke == /\ s.remote = "terminating" /\ s.gen > 0
    /\ s.attempts[s.gen].stage = "authorized"
    /\ s' = [s EXCEPT !.attempts[s.gen] =
        [stage |-> "settled", live |-> FALSE, owner |-> "none"]]
Acquire == /\ s.gen > 0 /\ s.attempts[s.gen].stage = "producing"
    /\ s' = [s EXCEPT !.lateAcquired = s.remote = "terminating",
        !.attempts[s.gen] = [stage |-> "owned", live |-> TRUE,
        owner |-> IF Broken = "lost-owner" THEN "none"
            ELSE IF s.remote = "starting" THEN "helper" ELSE "producer"]]
SpawnFailure == /\ s.gen > 0 /\ s.attempts[s.gen].stage = "producing"
    /\ s.remote = "starting"
    /\ s' = [s EXCEPT !.attempts[s.gen] =
        [stage |-> "settled", live |-> FALSE, owner |-> "none"],
        !.retry = TRUE]
Retryable == /\ s.gen > 0 /\ s.attempts[s.gen].stage \in {"producing", "owned"}
    /\ s.remote = "starting" /\ ~s.retry
    /\ s' = [s EXCEPT !.retry = TRUE, !.evidence = FALSE]
Retry == /\ s.gen = 1 /\ s.remote = "starting" /\ s.retry /\ Cut
    /\ (AllSettled \/ Broken = "early-retry") /\ s.primary = "none"
    /\ s' = [s EXCEPT !.gen = 2, !.retry = FALSE, !.evidence = FALSE,
        !.deadline = FALSE, !.badRetry = ~AllSettled,
        !.observedOvertake = @ \/ s.loss \in {"recognized", "admitted"},
        !.retryOvertake = s.loss \in {"recognized", "admitted"},
        !.attempts[2] = [stage |-> "authorized", live |-> FALSE, owner |-> "producer"]]
Exhausted == /\ s.gen = 2 /\ s.retry /\ AllSettled /\ s.remote = "starting"
    /\ s' = End(Primary(s, "startup"), "failure")
ReadinessEvidence == /\ Mode = "server" /\ s.gen > 0 /\ s.remote = "starting"
    /\ s.attempts[s.gen].stage = "owned" /\ ~s.retry /\ ~s.evidence
    /\ s' = [s EXCEPT !.evidence = TRUE]
Ready == /\ s.remote = "starting" /\ s.evidence /\ ~s.retry /\ Cut
    /\ ~s.writerBroken /\ Len(s.wire) < 2 /\ ~s.terminal
    /\ s' = [s EXCEPT !.remote = "ready", !.wire = Append(@, "READY"),
        !.protocolLog = Append(@, "READY"),
        !.readyOvertake = s.loss \in {"recognized", "admitted"},
        !.observedOvertake = @ \/ s.loss \in {"recognized", "admitted"},
        !.lateReady = s.local = "cancelling"]
Forward == /\ Mode = "server" /\ s.local = "opening" /\ s.readySeen /\ ~s.forwarded /\ s.transport
    /\ s' = [s EXCEPT !.forwarded = TRUE]
LaunchClient == /\ Mode = "server" /\ ~s.client /\ s.readySeen /\ s.forwarded /\ s.transport
    /\ (s.local = "opening" \/ Broken = "late-client")
    /\ s' = [s EXCEPT !.local = "active", !.client = TRUE,
        !.badClient = s.local # "opening"]

Exit == /\ s.gen > 0 /\ s.attempts[s.gen].stage = "owned"
    /\ s.remote \in {"starting", "ready", "terminating"}
    /\ s.childResult = <<>>
    /\ LET outcome == IF s.remote = "starting" /\ Mode = "server"
                      THEN Primary(s, "early-exit")
                      ELSE IF ExitCode # 0 /\ s.remote # "terminating"
                      THEN Primary(s, "openocd") ELSE s
       IN s' = [End(outcome, "process-exit") EXCEPT !.childResult = <<ExitCode>>]
Signal == /\ ~s.signalUsed /\ s.remote # "ended"
    /\ s' = [End(s, "signal") EXCEPT !.signalUsed = TRUE]
Deadline == /\ Mode = "server" /\ s.remote = "starting" /\ ~s.deadline
    /\ s' = [s EXCEPT !.deadline = TRUE]
TimeoutDetermination == /\ s.remote = "starting" /\ s.deadline /\ ~s.evidence
    /\ s' = End(Primary(s, "timeout"), "failure")
WriterFailure == /\ ~s.writerBroken /\ s.remote # "ended"
    /\ s' = [End(Primary(s, "output"), "failure") EXCEPT
        !.writerBroken = TRUE, !.wire = <<>>]
ObserverFailure == /\ ~s.observerFailed /\ s.remote # "ended"
    /\ s' = [End(Primary(s, "observer"), "failure") EXCEPT !.observerFailed = TRUE]
Cleanup(g) == /\ s.attempts[g].stage = "owned"
    /\ (s.remote = "terminating" \/ (g = s.gen /\ s.retry))
    /\ s' = [s EXCEPT !.attempts[g] =
        [stage |-> "settled", live |-> FALSE, owner |-> "none"]]
CleanupFailure(g) == /\ s.attempts[g].stage = "owned"
    /\ (s.remote = "terminating" \/ (g = s.gen /\ s.retry))
    /\ LET operation == IF s.retry THEN Primary(s, "startup") ELSE s
           failure == IF Broken = "lost-diagnostic" THEN operation
                      ELSE Primary(operation, "cleanup")
           result == IF Broken = "replace-primary" THEN
                         [failure EXCEPT !.primary = "cleanup"] ELSE failure
       IN s' = [End(result, "failure") EXCEPT !.cleanupErrors = @ + 1,
            !.attempts[g].stage = "settled", !.attempts[g].owner = "helper"]
StaleObservation == /\ s.gen = 2 /\ ~s.staleSeen
    /\ s' = [s EXCEPT !.staleSeen = TRUE,
        !.evidence = IF Broken = "stale-adopt" THEN TRUE ELSE @,
        !.badStale = Broken = "stale-adopt"]

CommitTerminal == /\ s.remote = "terminating" /\ AllSettled /\ ~s.terminal
    /\ s' = [s EXCEPT !.terminal = TRUE, !.terminalCount = @ + 1,
        !.terminalPrimary = s.primary, !.terminalDiagnostics = s.diagnostics,
        !.protocolLog = Append(@, "ENDED"),
        !.resultSource = IF Broken = "status-mix" THEN "child"
            ELSE IF s.childResult # <<>> THEN "child" ELSE "none",
        !.resultValue = IF s.childResult # <<>> THEN s.childResult
            ELSE IF Broken = "status-mix" THEN <<255>> ELSE <<>>]
AdmitTerminal == /\ s.terminal /\ s.remote = "terminating" /\ ~s.writerBroken
    /\ "ENDED" \notin {s.wire[i] : i \in 1..Len(s.wire)}
    /\ ~s.endedReceived /\ Len(s.wire) < 2
    /\ s' = [s EXCEPT !.wire = Append(@, "ENDED")]
Deliver == /\ Len(s.wire) > 0 /\ ~s.writerBroken
    /\ LET received == IF Head(s.wire) = "ENDED" /\ s.terminalPrimary # "none"
                       THEN LocalPrimary(s, s.terminalPrimary) ELSE s
       IN s' = [received EXCEPT !.wire = Tail(@),
        !.localDiagnostics = IF Head(s.wire) = "ENDED"
            THEN @ \o s.terminalDiagnostics ELSE @,
        !.readySeen = IF Head(s.wire) = "READY" /\ s.local = "opening" THEN TRUE ELSE @,
        !.endedReceived = IF Head(s.wire) = "ENDED" THEN TRUE ELSE @,
        !.localChild = IF Head(s.wire) = "ENDED" /\ s.resultSource = "child"
                      THEN s.resultValue ELSE @,
        !.local = IF Head(s.wire) = "ENDED" THEN "ended" ELSE @]
CloseRemote == /\ s.terminal /\ s.remote = "terminating"
    /\ (s.writerBroken \/ s.endedReceived)
    /\ s' = [s EXCEPT !.remote = "ended"]
CloseLocal == /\ s.local = "cancelling" /\ s.remote = "ended"
    /\ s' = [s EXCEPT !.local = "ended"]
SecondTerminal == /\ Broken = "second-terminal" /\ s.terminal
    /\ s.terminalCount = 1 /\ s' = [s EXCEPT !.terminalCount = 2]

Produce(stream) == /\ s.remote \in {"starting", "ready"} /\ ~s.terminal
    /\ s.produced[stream] < MaxChunks /\ Len(s.bulk[stream]) < 1
    /\ s' = [s EXCEPT !.produced[stream] = @ + 1,
        !.bulk[stream] = Append(@, s.produced[stream] + 1)]
Relay(stream) == /\ Len(s.bulk[stream]) > 0
    /\ s' = [s EXCEPT !.delivered[stream] = Head(s.bulk[stream]),
        !.bulk[stream] = Tail(@),
        !.badOrdering = @ \/ Head(s.bulk[stream]) # s.delivered[stream] + 1]

Next == Start \/ LocalCancel \/ LocalFailure \/ TransportLoss \/ RecognizeLoss \/ AdmitLoss \/ HandleLoss
    \/ Dispatch \/ Revoke \/ Acquire \/ SpawnFailure \/ Retryable \/ Retry \/ Exhausted
    \/ ReadinessEvidence \/ Ready \/ Forward \/ LaunchClient \/ Exit \/ Signal
    \/ Deadline \/ TimeoutDetermination \/ WriterFailure \/ ObserverFailure \/ StaleObservation
    \/ (\E g \in Attempts : Cleanup(g) \/ CleanupFailure(g))
    \/ CommitTerminal \/ AdmitTerminal \/ Deliver \/ CloseRemote \/ CloseLocal
    \/ SecondTerminal \/ (\E stream \in Streams : Produce(stream) \/ Relay(stream))
Spec == Init /\ [][Next]_vars
FairSpec == Spec /\ WF_vars(RecognizeLoss) /\ WF_vars(AdmitLoss) /\ WF_vars(HandleLoss)
    /\ WF_vars(Revoke) /\ WF_vars(Acquire) /\ WF_vars(CommitTerminal)
    /\ WF_vars(AdmitTerminal) /\ WF_vars(Deliver) /\ WF_vars(CloseRemote)
    /\ (\A g \in Attempts : WF_vars(Cleanup(g)))
WorkersUnfairSpec == Spec /\ WF_vars(RecognizeLoss) /\ WF_vars(AdmitLoss)
    /\ WF_vars(HandleLoss) /\ WF_vars(CommitTerminal) /\ WF_vars(AdmitTerminal)
    /\ WF_vars(Deliver) /\ WF_vars(CloseRemote)

LocalSafety == ~s.badClient /\ (s.client => s.readySeen /\ s.forwarded)
ReadinessHonest == s.remote = "ready" => s.attempts[s.gen].stage = "owned"
    /\ s.attempts[s.gen].live
TerminalSafety == ~s.badAttempt /\ (s.remote \in {"terminating", "ended"} => ~s.retry)
ResourceOwned == \A g \in Attempts : s.attempts[g].live => s.attempts[g].owner # "none"
RetrySettled == ~s.badRetry
NoStaleMutation == ~s.badStale
PrimaryPreserved == s.established = "none" \/ s.primary = s.established
LocalPrimaryPreserved == s.localEstablished = "none" \/ s.localPrimary = s.localEstablished
LocalResultHonest == s.localChild = <<>> \/ s.localChild = s.childResult
ChildFailureRecorded == ExitCode # 0 /\ s.childResult # <<>>
    /\ s.cause = "process-exit" => s.primary # "none"
OneShotSafety == Mode = "oneshot" => ~s.client /\ s.remote # "ready"
DiagnosticsRetained == s.cleanupErrors <=
    Cardinality({i \in 1..Len(s.diagnostics) : s.diagnostics[i] = "cleanup"})
        + IF s.primary = "cleanup" THEN 1 ELSE 0
ResultHonest == s.resultSource = "child" => s.childResult # <<>> /\ s.resultValue = s.childResult
TerminalUnique == s.terminalCount <= 1
ProtocolClosed == s.terminal => s.protocolLog[Len(s.protocolLog)] = "ENDED"
ClosureAccountsResources == s.remote = "ended" => AllSettled
OutputBoundedOrdered == Len(s.wire) <= 2 /\ ~s.badOrdering
    /\ \A stream \in Streams : Len(s.bulk[stream]) <= 1
ObservedPrecedence == ~s.observedOvertake
NoLateReadyWitness == ~(s.lateReady /\ s.remote = "ended" /\ AllDisposed
    /\ s.loss = "handled" /\ s.cause = "controller-ended")
NoReadyOvertakeWitness == ~s.readyOvertake
NoRetryOvertakeWitness == ~s.retryOvertake
NoRequestedWinner == ~(s.cause = "controller-ended" /\ s.childResult # <<>>
    /\ s.remote = "ended" /\ AllDisposed)
NoNaturalWinner == ~(s.cause = "process-exit" /\ s.loss = "handled"
    /\ s.remote = "ended" /\ AllDisposed)
NoLateAcquireWitness == ~(s.lateAcquired /\ s.remote = "ended" /\ AllDisposed
    /\ s.loss = "handled")
TerminationCloses == (s.remote = "terminating") ~> (s.remote = "ended")
=============================================================================
