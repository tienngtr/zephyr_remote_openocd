# fence-unresolved

Expected liveness counterexample: `FenceEnds`.
Rows are actual TLC state projections; queue entries carry source/index tokens.

| Step/action | Phase/gen; terminal | Admission r/a/h; mailbox | Epoch/request; proposal/gen | Hold/ack/receipt | Ready/retry/settled | Journal | Audit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 Initial predicate | "Starting"/1; FALSE | 1/1/1; FALSE | 0/0; "none"/0 | 0/0/0 | TRUE/FALSE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 2 ChildExit | "Retiring"/1; FALSE | 1/1/1; FALSE | 0/0; "none"/0 | 0/0/0 | FALSE/TRUE/FALSE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 3 Settle | "Retiring"/1; FALSE | 1/1/1; FALSE | 0/0; "none"/0 | 0/0/0 | FALSE/TRUE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 4 Begin | "Retiring"/1; FALSE | 1/1/1; FALSE | 1/1; "retry"/1 | 0/0/0 | FALSE/TRUE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 5 Hold | "Retiring"/1; FALSE | 1/1/1; FALSE | 1/1; "retry"/1 | 1/0/0 | FALSE/TRUE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 6 PublishAck | "Retiring"/1; FALSE | 1/1/1; FALSE | 1/1; "retry"/1 | 1/1/0 | FALSE/TRUE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |
| 7 ReceiveAck | "Retiring"/1; FALSE | 1/1/1; FALSE | 1/1; "retry"/1 | 1/0/1 | FALSE/TRUE/TRUE | <<>> | [ retarget &#124;-> FALSE, stopOvertake &#124;-> FALSE, fenceAborted &#124;-> FALSE, stopDuringCleanup &#124;-> FALSE ] |

TLC closes this counterexample with a repeating/stuttering cycle.
