# unfair-liveness

Expected liveness counterexample: `TerminationCloses`.
Each row is a projection of an actual TLC state; r/a/h means recognized/admitted/handled.

| Step/action | Phase/gen | Intent/primary | Control; interrupt r/a/h | Barrier | Tickets (owner) | Protocol | Secondary diagnostics | Late rejection |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Initial predicate | Created/0 | none/none | 0/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 2 Recognize | Created/0 | none/none | 2/0/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 3 Admit | Created/0 | none/none | 2/1/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 4 Admit | Created/0 | none/none | 2/2/0; 0/0/0 | idle | — | SESSION_CREATED(0) | — | — |
| 5 Handle | Starting/1 | none/none | 2/2/1; 0/0/0 | idle | 1/child:authorized(effect) | SESSION_CREATED(0) | — | — |
| 6 DispatchEffect | Starting/1 | none/none | 2/2/1; 0/0/0 | idle | 1/child:running(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 7 CompleteEffect | Starting/1 | none/none | 2/2/1; 0/0/0 | idle | 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 8 AttemptExit | Retiring/1 | failure/operational | 2/2/1; 0/0/0 | idle | 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 9 BeginCleanup | Retiring/1 | failure/operational | 2/2/1; 0/0/0 | idle | 1/child:offered(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 10 DispatchCleanup | Retiring/1 | failure/operational | 2/2/1; 0/0/0 | idle | 1/child:cleaning(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | — | — |
| 11 FinishCleanup | Retiring/1 | failure/operational | 2/2/1; 0/0/0 | idle | 1/child:settled(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | extra | — |
| 12 Handle | Retiring/1 | failure/operational | 2/2/2; 0/0/0 | idle | 1/child:settled(effect) | SESSION_CREATED(0), PROCESS_STARTING(1) | extra | — |

TLC closes this counterexample with a repeating/stuttering cycle.
