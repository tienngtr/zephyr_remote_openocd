---------------------------- MODULE ProductHistories ----------------------------
EXTENDS ProductSession
CONSTANT History
VARIABLE step, last
historyVars == <<s, step, last>>
ReadyHistory == <<"start", "dispatch", "acquire", "evidence", "cancel", "ready",
    "deliver", "deliver", "recognize", "admit", "handle", "cleanup",
    "terminal", "enqueue", "deliver", "close">>
RequestedHistory == <<"start", "dispatch", "acquire", "evidence", "ready",
    "deliver", "deliver", "cancel", "recognize", "admit", "handle", "exit",
    "cleanup", "terminal", "enqueue", "deliver", "close">>
NaturalHistory == <<"start", "dispatch", "acquire", "evidence", "ready",
    "deliver", "deliver", "exit", "cancel", "recognize", "admit", "handle",
    "cleanup", "terminal", "enqueue", "deliver", "close">>
LateAcquireHistory == <<"start", "dispatch", "deliver", "cancel", "recognize",
    "admit", "handle", "acquire", "cleanup", "terminal", "enqueue", "deliver", "close">>
Plan == CASE History = "benign-ready" -> ReadyHistory
    [] History = "benign-requested" -> RequestedHistory
    [] History = "benign-natural" -> NaturalHistory
    [] History = "benign-late-acquire" -> LateAcquireHistory
Take(name) == CASE name = "start" -> Start [] name = "dispatch" -> Dispatch
    [] name = "acquire" -> Acquire [] name = "evidence" -> ReadinessEvidence
    [] name = "cancel" -> LocalCancel [] name = "ready" -> Ready
    [] name = "deliver" -> Deliver [] name = "recognize" -> RecognizeLoss
    [] name = "admit" -> AdmitLoss [] name = "handle" -> HandleLoss
    [] name = "cleanup" -> Cleanup(1) [] name = "terminal" -> CommitTerminal
    [] name = "enqueue" -> AdmitTerminal [] name = "close" -> CloseRemote
    [] name = "exit" -> Exit
HistoryInit == Init /\ step = 0 /\ last = "initial"
HistoryNext == /\ step < Len(Plan) /\ Take(Plan[step + 1]) /\ step' = step + 1
    /\ last' = Plan[step + 1]
HistorySpec == HistoryInit /\ [][HistoryNext]_historyVars
NoCompleteWitness == step < Len(Plan)
HistorySound == step = Len(Plan) => s.remote = "ended" /\ s.local = "ended"
    /\ AllDisposed /\ s.primary = "none" /\ ~s.client
=============================================================================
