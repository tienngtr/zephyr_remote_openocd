# Differential behavior matrix

Corrected main is `ce12b6a`. Stock reference is Zephyr 4.4.0 runner/core at the
commit pinned in REPORT. This is a requirements/source/test comparison, not a
claim that the stand-in ran west or target hardware. **Required** means eventual
production equivalence; **improvement** is deliberately different semantics;
**benign** permits either result; **remote** has no stock equivalent;
**gap** identifies omitted functionality, which would be a regression if this
slice were deployed unchanged.

| Case | Stock-compatible desired behavior | Corrected main | Candidate 3 evidence | Classification |
| --- | --- | --- | --- | --- |
| Flash success/failure | Checked one-shot argv; genuine zero/nonzero result | Session reports genuine child status; infrastructure distinct | Real stand-in exit 0/7, exact ATTEMPT, real helper status separate | Required; target/command generation gap |
| Debug startup | Local GDB after usable server and transport | Marker startup, required forwards, stock GDB argv/init | Actual marker decoding, live poll, READY admission; gate tests both prerequisites | Required; actual GDB/ports gap |
| Attach | Local GDB connects without load; same initialization policy as stock | Zephyr adapter preserves attach plan | Same session/gate primitives apply; no target attach performed | Required; gap |
| Debugserver | Usable foreground endpoint; no GDB launch | Remote ready plus required forward; foreground supervision | Real long-lived stand-in and owned cleanup, no dependent launch needed | Required; real endpoint gap |
| RTT shape | Setup then RTT client, local client/semihosting behavior preserved | GDB setup then RTT required/GDB best effort; local RTT owner | Opening/readiness gate reusable per local phase; deliberately no RTT inside helper | Required; RTT phase integration gap |
| Cancellation during startup | Cancel operation and dispose partial owned server | STOP/EOF and pending-success reconciliation | Real stdin EOF during held returned-process handoff; no READY, late ticket disposed | Required |
| Cancellation after READY | Close owned session, independent cleanup | STOP then structured closure | Real EOF, child group reap, final stdout survives | Required |
| Late READY | Cancellation forbids new GDB/client | Remote suppresses success for recognized pending termination | Actual remote READY after local cancel permitted; actual launch callback rejects | Improvement / benign |
| Natural exit vs shutdown | Either initiating cause; preserve actually observed child result | Event handling chooses initiating outcome; v1 explicitly allows either final closure form | Both deterministic dispatch orders over real exit and real EOF; one snapshot | Benign |
| Startup timeout | Fail unusable startup, no dependent launch | Reader checkpoints/final deadline, possible evidence accepted | Actual timer future triggers bounded pipe prefix/EOF/poll; missing evidence fails | Required; observation cut is policy |
| Marker at deadline | Avoid accidental loss from reader scheduling | Final observations can win timeout determination | Ordinary readers paused; actual kernel marker bytes accepted by final scan | Required under proposed final-observation policy |
| Child exit at deadline | Reapable dead server cannot authorize live launch | Final child/reader observation | Ordinary exit observer paused, real pidfd exit, final poll preserves status 9 | Required |
| Output EOF at deadline | Account final decoder/line evidence; missing policy fails | Reader EOF processed during final checkpoint | Actual close-output, real nonblocking EOF; separate EOF-completed markers reach READY | Required |
| Controller EOF at deadline | Cleanup; timeout/shutdown race may have either cause | Stronger pending-controller ordering | Actual pipe EOF and timer expiry; no fabricated child status | Benign / remote |
| Child exit before readiness | No dependent live client, useful failure and status | Startup failure; bind classification may retry | Reapable exit before READY; live poll at success; genuine status distinct | Required |
| Bind retry | Remote allocation recovery only when safely repeatable | Address allocation plus old group/reader cleanup and control fence | Controlled bind failure, real cleanup, attempt 2; internal generation | Remote; dynamic address classifier gap |
| Retry before EOF handled | Permitted while helper still requires session by its knowledge | Recognized pending EOF defeats retry | Actual EOF while observer paused; attempt 2 permitted, then EOF cleans both | Improvement / benign |
| Retry after termination | Forbidden even if same generation | ending state and fence checks | Entry checks phase; pending producer prevents retry; terminal-entry mutation fails | Required safety |
| Stale attempt result | No current ownership/readiness change | Child identity fences | Late old offer rejected; stale-generation mutation fails | Required safety |
| SIGINT/SIGTERM | Startup/foreground cancellation; GDB Ctrl-C stays GDB interaction | Native session latch plus runner-specific interrupt handling | Real native/helper signals, handoff capture and group cleanup | Required; interactive GDB integration gap |
| Output failure | Operation/infra failure with cleanup; keep primary | `_ProtocolOutput`, failed emit/drain composition | Actual EPIPE after spawn, blocked pipe and final drain; process/workspace cleanup independent | Required |
| Partial terminal write | Cannot promise delivery/replay safely | Failed terminal output visible locally | Real short write then EPIPE; single snapshot, no replay or second decision | Required safety / remote |
| ATTEMPT admission failure | No attempted work lacking admitted diagnostic | PROCESS_STARTING admission protects spawn | Capacity failure prevents actual Popen; failed spawn still exposes exact argv | Required; frame becomes diagnostic |
| READY admission failure | No claimed readiness without local output admission | Ready emit failure prevents success | Full local buffer causes termination, no committed READY | Required |
| Cleanup failure | Fatal if no prior failure; otherwise secondary; don't abandon other owners | Exception/error precedence and note composition | Actual staging lease failure, nested secondary record plus EPIPE retains primary; child cleanup still succeeds | Required |
| Transport loss | Local failure; independent remote cleanup after remote observation | Separate local/remote status checks; no instantaneous guarantee | Real pipe/SSH/mux client death; helper reaped, child gone, workspace removed | Remote necessity |
| Local shutdown timeout | Finite effort; never claim disconnected remote cleanup | Bounded owned transport cleanup | Actual ignored TERM, deterministic local budgets, KILL/reap; disposal unconfirmed | Required remote safety |
| Zero helper status without final result | Missing final result is infrastructure uncertainty, not OpenOCD success | Client protocol/transport checks fail incomplete session | Real stdout EOF, helper reaped with zero, local outcome fails and disposal remains unconfirmed | Required provenance |
| Nonzero helper after valid result | Infrastructure result separate from child status | Session independently checks helper/transport | Actual helper-like process exits 5 after valid snapshot; child remains None | Required provenance |
| Final result delivery failure | Cleanup still occurs; client reports unavailable result | ERROR/SESSION_CLOSED delivery may fail | Single Closed snapshot; original writer diagnostic local; SSH abort checks cleanup separately | Remote necessity |
| Workspace/staging overlap | Active staging not destroyed; reject new stage; finite failed wait | SH/EX exclusion and closed admission | Real separate staging process, release receipt, workspace valid until release; timeout leaves closed residual | Required safety |
| Workspace vs live child | Staged inputs survive all legitimate process use, including shutdown | Workspace release follows child/group cleanup | Real child cwd, pending producer/ignored TERM retains workspace; removal waits original disposal response | Required safety; uncertain child disposal retains workspace conservatively |
| Final retained bulk output | Preserve useful FIFO tail under finite remote output policy | Relay accounting and physical drain, bounded output adapter | Actual 130000-byte burst, blocked peer, cleanup finishes first; peer resumes and receives complete tail then one terminal snapshot | Required output behavior / remote bounded policy |
| Workspace metadata reclamation | Safe inactive/orphan reclaim, no unsafe late-stage recreation | Existing reclamation/lease implementation | Intentionally retained test tombstones; no general reclaim | Gap; retain production mechanism |

No tested case establishes a need for recognized-but-unadmitted shutdown to beat
success. No tested safety property is weakened by permitting remote late READY
or a retry before remote authority receives EOF. The failure cases instead
confirm why local launch validation, resource ownership, genuine settlement,
physical final observation and explicit cleanup uncertainty remain necessary.
