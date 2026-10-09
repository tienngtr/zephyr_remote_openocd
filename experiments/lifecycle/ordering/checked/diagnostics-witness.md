# diagnostics-witness

Expected reachable witness: `NoDiagnosticsWitness`.
Each row is a projection of an actual TLC state; r/a/h means recognized/admitted/handled.

| Step/action | Phase/gen | Intent/primary | Control; interrupt r/a/h | Barrier | Tickets (owner) | Protocol | Secondary diagnostics | Late rejection |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Initial predicate | Created/0 | none/none | 0/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 2 Recognize | Created/0 | none/none | 1/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 3 Admit | Created/0 | none/none | 1/1/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 4 Handle | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:authorized(effect) | SESSION_CREATED(0) | — | — |
| 5 DispatchEffect | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:running(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 6 CompleteEffect | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 7 DispatchEffect | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:running(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 8 AttemptExit | Retiring/1 | failure/operational | 1/1/1; 0/0/0 | idle | 1/aux:running(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 9 CompleteEffect | Retiring/1 | failure/operational | 1/1/1; 0/0/0 | idle | 1/aux:offered(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 10 BeginCleanup | Retiring/1 | failure/operational | 1/1/1; 0/0/0 | idle | 1/aux:offered(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 11 DispatchCleanup | Retiring/1 | failure/operational | 1/1/1; 0/0/0 | idle | 1/aux:offered(effect), 1/child:cleaning(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 12 FinishCleanup | Retiring/1 | failure/operational | 1/1/1; 0/0/0 | idle | 1/aux:offered(effect), 1/child:settled(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | nested [stream-close-detail retained] | — |
| 13 DispatchCleanup | Retiring/1 | failure/operational | 1/1/1; 0/0/0 | idle | 1/aux:cleaning(effect), 1/child:settled(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | nested [stream-close-detail retained] | — |
| 14 FinishCleanup | Retiring/1 | failure/operational | 1/1/1; 0/0/0 | idle | 1/aux:settled(effect), 1/child:settled(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | nested, extra [stream-close-detail retained] | — |
| 15 Close | Closed/1 | failure/operational | 1/1/1; 0/0/0 | idle | 1/aux:settled(effect), 1/child:settled(effect) | SESSION_CREATED(0), PROCESS_STARTING(1), ERROR(1) | nested, extra [stream-close-detail retained] | — |
