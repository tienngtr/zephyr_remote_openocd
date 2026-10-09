# generation-only

Expected counterexample: `NoInvalidExecution`.
Each row is a projection of an actual TLC state; r/a/h means recognized/admitted/handled.

| Step/action | Phase/gen | Intent/primary | Control; interrupt r/a/h | Barrier | Tickets (owner) | Protocol | Secondary diagnostics | Late rejection |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Initial predicate | Created/0 | none/none | 0/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 2 Recognize | Created/0 | none/none | 2/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 3 Admit | Created/0 | none/none | 2/1/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 4 Admit | Created/0 | none/none | 2/2/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 5 HandleBatch | Retiring/1 | STOP/none | 2/2/2; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:authorized(effect) | SESSION_CREATED(0) | — | — |
| 6 DispatchEffect | Retiring/1 | STOP/none | 2/2/2; 0/0/0 | idle | 1/aux:authorized(effect), 1/child:running(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
