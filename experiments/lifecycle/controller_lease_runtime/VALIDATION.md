# Executed validation

Checked on 2026-10-10 with repository `.venv/bin/python` **3.14.7** on Linux.
The implementation targets Python 3.12+; Python 3.12 itself was not executed.
No OpenOCD, GDB, target, configured lab host, or hardware inventory was used.

Branch: `experiment/controller-lease-runtime`, based on parent experiment
`b74f093b36641180d2da4c274772fc6fffdf3593`. Corrected production baseline:
`ce12b6a2e7dd00b4b579f70e942c0e5f8ad39bc5`. Stock reference: Zephyr 4.4.0
`684c9e8f32e4373a21098559f748f06915f950c9`, inspected in the supported local
checkout. Production, normative documents and previous experiment directories
remain unchanged. Results below apply to the new experimental directory.

## Checks

Commands were run from the repository root. Redirection to ignored scratch logs
is omitted here; no scratch keys, hosts or populated inventories are deliverables.

| Executed command | Result |
| --- | --- |
| `.venv/bin/python -m pytest experiments/lifecycle/controller_lease_runtime -q -W error --timeout=60` | **56 passed**; real pipes, processes, groups, signals, locks and deterministic clock/receipt seams |
| `.venv/bin/python -m experiments.lifecycle.controller_lease_runtime.mutations` | **8 passing baselines, 8 rejected mutants**; each mutant fails a semantic assertion, not the deadlock timeout |
| `PYTHONPATH=python .venv/bin/python -m experiments.lifecycle.controller_lease_runtime.verify_transport --openssh` | **6 checked cases**: pipe proxy, ordinary OpenSSH, ControlMaster, each intentional half-close and unexpected local client death |
| `.venv/bin/python -m pytest tests/unit -q` | **565 passed** against unchanged production |
| `.venv/bin/python -m ruff check experiments/lifecycle/controller_lease_runtime` | Passed |
| `.venv/bin/python -m ruff format --check experiments/lifecycle/controller_lease_runtime` | Passed |
| `.venv/bin/python -m mypy --config-file=mypy.ini experiments/lifecycle/controller_lease_runtime` | Passed, all 15 experimental Python files |
| `.venv/bin/python scripts/contributor/static_check.py` | Passed repository static checks |

The production unit suite initially encountered sandbox socket restrictions;
the successful rerun permitted its local sockets. The transport check likewise
needed permission for its local ControlMaster Unix socket. It uses generated
scratch keys and an inetd SSH server over pipes; it opens no network listener
and contacts no remote host. The tested client is OpenSSH 10.6p1. This qualifies
that transport fixture, not every SSH version or arbitrary wrapper.

Generated [MUTATIONS_CHECKED.json](MUTATIONS_CHECKED.json) records the selected
nodes, baseline/mutant results and SHA-256 hashes of every experimental Python
file. [TRANSPORT_CHECKED.json](TRANSPORT_CHECKED.json) records receipt and
independently observed cleanup per transport case. In deliberate transport-loss
cases, failure to receive a final snapshot is expected; cleanup is checked by
actual helper exit, child absence and workspace removal instead.

## Behavior exercised

| Obligation | Checked scenarios |
| --- | --- |
| Lease rather than STOP | Actual START bytes, open stdin, actual EOF; helper's final stdout after EOF; real helper process and configured managed SSH processes |
| Local cancellation revokes launch | Queued actual launch callback after Cancelling; both readiness and forwarding required; active local process remains owned until reaped |
| Benign remote late success | Real remote READY after local cancellation; retry while real EOF remains unread; later EOF disposes owned processes; both natural-exit/shutdown dispatch orders |
| Authority termination revokes attempts | Accounted EOF defeats READY/retry; direct attempted entry after Terminating rejected |
| Continuous physical custody | BaseException at returned/acquired/before-adopt/after-adopt; native signal during synchronous handoff; late producer response after EOF; original ticket remains reachable |
| Settlement and generations | Real retryable child exit, held final producer response, timeout while producer pending, attempt 2 only after disposal, stale duplicate offer rejected |
| Group disposal | Actual ignoring-SIGTERM child, explicit escalation deadline, SIGKILL/reap; forked descendant and leader exit; pidfd exit and unreaped group reservation |
| Readiness and final observation | Split UTF-8, fragmented/newline-free output, required markers across streams, EOF-completed marker; actual kernel bytes/child exit/stream EOF/controller EOF at deadline determination; empty observation timeout |
| Bounded output | Real pipe partial write and FIFO; ATTEMPT admission failure prevents Popen; READY admission failure prevents Active; EPIPE after spawn preserves cleanup; retained 130000-byte tail drains after resource disposal |
| One terminal snapshot | Failure before first terminal byte and after partial write; local writer diagnostics retained without replay or another terminal decision |
| Outcome provenance | Stand-in child exit 0/7, helper status separately 0/5, zero helper without terminal still fails; established primary survives nested cleanup details and writer failure |
| Bounded local shutdown | Actual stdin closure and managed-process termination; ignored TERM then KILL; local timeout reports unconfirmed remote cleanup |
| Workspace dependencies | Real child cwd; input retained while producer/child live; separate staging process validates under SH flock; closed admission precedes EX removal; stage timeout leaves workspace and still disposes child |
| Boundary encoding | Surrogate-escaped native argv survives immutable JSON admission; missing executable remains an operation failure with workspace cleanup |

Tests use ordinary initialization and typed seams at real process/FD boundaries.
Clock expiry controls deadline determination; it does not supply the resulting
marker, EOF, child status or cleanup response. Handshakes are actual bytes,
receipts, descriptor readiness, process exit, locks, events and task completion.
There are no sleeps, elapsed-time assertions or shortened production deadlines.
The 60-second pytest timeout is only a deadlock safety net. Failed mutation tests
have independent fixture rescue for their real acquired processes.

## Negative checks

| Deliberate weakening | Semantic failure observed |
| --- | --- |
| Launch after local Cancelling | Real dependent-launch invocation appears |
| Retry before producer settlement | Next generation starts while original producer remains capable |
| Adopt stale offer | Current generation/owned ticket changes to retired attempt |
| Drop original acquisition owner | Acquired child remains outside lifecycle cleanup |
| Replace primary with cleanup failure | Established operational diagnostic is replaced |
| Enter attempt after remote termination | Actual new process is created |
| Emit second terminal result | Terminal commitment count exceeds one |
| Treat local timeout as remote success | Unconfirmed remote disposal is reported as confirmed |

## Limits and progress assumptions

These are deterministic real-boundary tests, not exhaustive state exploration
or proof of unconditional distributed liveness. Progress assumes a scheduled
loop, genuine producer final response, OS signal/reaping progress, and staging
or output completion or a reported unsuccessful bounded wait. Timeout alone
never proves producer quiescence or remote disposal. Unconfirmed child disposal
retains dependent workspace inputs and reports residual responsibility.

No external pytest profiles or target-destructive actions were run. The matrix
marks actual Zephyr/OpenOCD/attach/debug/RTT/address allocation and secure
deployment integration as gaps. Foreign throwing signal handlers, failure
inside CPython before Popen returns a handle, fatal helper death and escaping
descendants are not proven safe by handoff hooks. Pre-announcement helper setup
and standalone packaging are not exhaustively qualified. Preserve production
physical protections during any subsequent redesign; do not deploy this slice
as a runner replacement.
