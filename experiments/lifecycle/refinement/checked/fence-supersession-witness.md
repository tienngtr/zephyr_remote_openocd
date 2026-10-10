# fence-supersession-witness

Expected reachable witness: `NoRetireStopWitness`.
Rows are actual TLC state projections; queue entries carry source/index tokens.

| Step/action | Phase/gen; terminal | Admission r/a/h; mailbox | Epoch/request; proposal/gen | Hold/ack/receipt | Ready/retry/settled | Journal | Audit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Initial predicate | "Starting"/1; FALSE | 1/1/1; FALSE | 0/0; "none"/0 | 0/0/0 | TRUE/FALSE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 2 Begin | "Starting"/1; FALSE | 1/1/1; FALSE | 1/1; "ready"/1 | 0/0/0 | TRUE/FALSE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 3 Hold | "Starting"/1; FALSE | 1/1/1; FALSE | 1/1; "ready"/1 | 1/0/0 | TRUE/FALSE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 4 ChildExit | "Retiring"/1; FALSE | 1/1/1; FALSE | 1/0; "none"/0 | 0/0/0 | FALSE/TRUE/FALSE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> TRUE, stopDuringCleanup &#124;-> FALSE ] |
| 5 RecognizeStop | "Retiring"/1; FALSE | 2/1/1; FALSE | 1/0; "none"/0 | 0/0/0 | FALSE/TRUE/FALSE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> TRUE, stopDuringCleanup &#124;-> TRUE ] |
