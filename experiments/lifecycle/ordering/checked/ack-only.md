# ack-only

Expected counterexample: `NoObservedReadyOvertake`.
Each row is a projection of an actual TLC state; r/a/h means recognized/admitted/handled.

| Step/action | Phase/gen | Intent/primary | Control; interrupt r/a/h | Barrier | Tickets (owner) | Protocol | Secondary diagnostics | Late rejection |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Initial predicate | Created/0 | none/none | 0/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 2 Recognize | Created/0 | none/none | 1/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 3 Admit | Created/0 | none/none | 1/1/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 4 Handle | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:authorized(effect) | SESSION_CREATED(0) | — | — |
| 5 ReadyCandidate | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:authorized(effect) | SESSION_CREATED(0) | — | — |
| 6 DispatchEffect | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:running(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 7 CompleteEffect | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 8 AdoptEffect | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:adopted(session) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 9 RequestBarrier | Starting/1 | none/none | 1/1/1; 0/0/0 | requested | 1/aux:authorized(effect), 1/child:adopted(session) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 10 Acknowledge | Starting/1 | none/none | 1/1/1; 0/0/0 | acked | 1/aux:authorized(effect), 1/child:adopted(session) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 11 Recognize | Starting/1 | none/none | 2/1/1; 0/0/0 | acked | 1/aux:authorized(effect), 1/child:adopted(session) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 12 Success | Active/1 | none/none | 2/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:adopted(session) | SESSION_CREATED(0), PROCESS_STARTING(1), PROCESS_READY(1) | — | — |
