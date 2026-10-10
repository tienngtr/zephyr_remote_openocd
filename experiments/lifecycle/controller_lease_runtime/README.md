# Controller-lease runtime experiment

Branch: `experiment/controller-lease-runtime`, from `experiment/product-semantics`
(`b74f093`). Corrected production baseline: main (`ce12b6a`). Everything here is
experimental. No production, normative docs or previous experiments are changed.

Start with [REPORT.md](REPORT.md), [REQUIREMENTS_AUDIT.md](REQUIREMENTS_AUDIT.md),
[DIFFERENTIAL_MATRIX.md](DIFFERENTIAL_MATRIX.md),
[CHANGEABILITY.md](CHANGEABILITY.md), and [VALIDATION.md](VALIDATION.md).

## Run

Use the repository environment, Python 3.12+ on Linux. The executed environment
is recorded in VALIDATION. The code uses real pipes, pidfds, process groups,
waitid, flock and POSIX signals; it is not a portable production helper yet.

```sh
.venv/bin/python -m pytest experiments/lifecycle/controller_lease_runtime -q -W error --timeout=60
.venv/bin/python -m experiments.lifecycle.controller_lease_runtime.mutations
PYTHONPATH=python .venv/bin/python -m experiments.lifecycle.controller_lease_runtime.verify_transport --openssh
.venv/bin/python scripts/contributor/static_check.py
```

The optional explicit transport check requires OpenSSH client, server and keygen,
and permission to create a local ControlMaster Unix socket. It runs an inetd SSH
server over pipes, not a TCP listener or remote host. All keys, configs and logs
are ignored scratch artifacts. `TRANSPORT_CHECKED.json` and
`MUTATIONS_CHECKED.json` contain only generic results and source hashes.
Neither command edits a previous experiment. Their read-only transport setup
helpers are imported from the parent experiment.

Tests are ordinary pytest functions running real asyncio objects. There is no
asyncio plugin or runtime dependency. No sleep, shortened production budget,
elapsed-time assertion, trace injection or hardware is used for coordination.
Timer expiry is an explicit clock seam into actual determination/cleanup paths;
real byte/EOF/process observations are not faked. The 60-second test limit is a
deadlock safety net. Temporary stand-in commands and receipts are private pipes.
Negative variants are opt-in through an experiment-only environment switch;
fixture rescue independently kills/reaps acquired stand-ins even after an
assertion fails.

## Source map

| File | Role |
| --- | --- |
| `model.py` | Frozen tagged decisions, facts, structured outcome/diagnostics |
| `runtime.py` | Single helper lifecycle authority and same-loop effect entry |
| `unix.py` | Physical writer, signal capture, process scope, timers and ticket |
| `streams.py` | Incremental decoding, bounded marker matching and final scan |
| `workspace.py` | Separate process-shared staging exclusion and cleanup |
| `local.py` | Local tagged launch gate and bounded transport closure |
| `helper.py` | Real helper stdin/stdout entry point |
| `standin.py` | Harmless controllable real child/descendant |
| `harness.py` | Real test pipes/receipts/clock seams and independent rescue |
| `test_runtime.py` | Deadline, EOF, generation, resource, retry and signal histories |
| `test_boundaries.py` | Actual output backpressure/failure, local launch and staging |
| `test_process.py` | Separate helper process and status/result provenance |
| `verify_transport.py` | Configured SSH abstraction, ordinary SSH and sharing checks |
| `mutations.py` | Eight deliberately weakened rules with semantic rejection |

## Experimental wire

Helper announces workspace/helper identity with `SESSION_CREATED`. Controller
sends one JSON-line START containing `argv`, `required` markers, `policy` (`live`
or `exit`), and bounded `max_attempts`. It then keeps stdin open to retain the
lease. Closing stdin requests termination without closing reverse output.

`ATTEMPT` carries exact argv and generation immediately before actual Popen.
`READY` carries current generation/PID after live startup policy and successful
local admission. `CHILD_OUTPUT` carries generation, stream and incremental text.
`SESSION_ENDED` freezes trigger, genuine current child observation, primary,
ordered secondary diagnostics (including retired attempt observations), and
physical disposal confirmation. Post-terminal delivery failures only augment
the local outcome. Framing is descriptive for this slice, not a proposed full
normative protocol schema or production compatibility promise.
