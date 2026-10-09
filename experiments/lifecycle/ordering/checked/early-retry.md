# early-retry

Expected counterexample: `RetryAfterSettlement`.
Each row is a projection of an actual TLC state; r/a/h means recognized/admitted/handled.

| Step/action | Phase/gen | Intent/primary | Control; interrupt r/a/h | Barrier | Tickets (owner) | Protocol | Secondary diagnostics | Late rejection |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Initial predicate | Created/0 | none/none | 0/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 2 Recognize | Created/0 | none/none | 1/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 3 Admit | Created/0 | none/none | 1/1/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 4 Handle | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:authorized(effect) | SESSION_CREATED(0) | — | — |
| 5 DispatchEffect | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:running(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 6 CompleteEffect | Starting/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 7 AttemptExit | Retiring/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 8 BeginCleanup | Retiring/1 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 9 RequestBarrier | Retiring/1 | none/none | 1/1/1; 0/0/0 | requested | 1/aux:authorized(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 10 Acknowledge | Retiring/1 | none/none | 1/1/1; 0/0/0 | acked | 1/aux:authorized(effect), 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 11 Success | Starting/2 | none/none | 1/1/1; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:offered(effect), 2/aux:authorized(effect), 2/child:authorized(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
