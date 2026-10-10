# Product compatibility boundary

This classification was established before selecting a candidate protocol.
The comparison baseline is corrected main `ce12b6a`; the parent experiment is
`146debb`. No normative document is changed by this experiment.

## Inspected stock runner

The available Zephyr checkout identifies 4.4.0, commit
`684c9e8f32e4373a21098559f748f06915f950c9`. Its
`scripts/west_commands/runners/openocd.py` and `runners/core.py` were read,
including parser, capabilities, flash construction, attach/debug/RTT,
debugserver, command logging, and client interrupt/cleanup wrappers. The
[upstream 4.4.0 source](https://github.com/zephyrproject-rtos/zephyr/blob/v4.4.0/scripts/west_commands/runners/openocd.py)
is a public reference; the supported environment's actual runner is the option
and functional behavior baseline, as SRS OPT-001 specifies. Inspection is
read-only; this experiment does not call private runner APIs.

Stock flash invokes its generated OpenOCD command and checks its status. Debug
and attach create a server, run local GDB, and terminate/wait for the server in
finally. Debug adds load unless disabled; attach does not add that load.
Debugserver runs OpenOCD in the foreground. RTT uses batch GDB setup and a
foreground RTT client with terminal restoration and server cleanup. Core logs
the escaped command before invocation and ignores the parent's SIGINT during
interactive GDB. These facts inform compatibility; exact logs and incidental
quirks are not the product contract.

## A. Stock-compatible product behavior

| Required behavior | Source/SRS boundary |
| --- | --- |
| Flash, debug, attach, debugserver **and RTT** work through west | Stock capabilities; SCOPE-002, FLASH-001–005, DEBUG-001–007, RTT-001–007. |
| Inherit supported option names/value forms and functional command behavior | Stock parser and core common options; OPT-001–007. Includes serial, file type/file/address, erase/verify/verify-only, Tcl commands/config/search, GDB initialization/TUI/load, ports, no-init/halt/targets, target handle, RTT options. The versioned parser remains authoritative rather than freezing this list. |
| Reuse generated board arguments and artifacts; preserve fixed command argv | BOARD-001–003, CONFIG-020–021, OPT-006–007; path rewriting only at supported generated boundaries. |
| GDB, symbols and workspace stay local; debug initialization matches stock | REMOTE-004, DEBUG-001/007. Attach does not flash; debugserver does not launch GDB. |
| Fail a failed flash; preserve a genuine OpenOCD result | FLASH-005, HELP-004/009. Infrastructure statuses must never masquerade as OpenOCD statuses. |
| Show useful incremental output and effective attempted argv | Stock logging/output; CONFIG-022/023, HELP-006. Exact argv must be available **before each attempt**, including unsuccessful spawn and collision retry. Incidental stock stream suppression is not copied over the stronger SRS output requirement. |
| Preserve GDB Ctrl-C interaction; cancel flash/RTT/debugserver on user interruption | Core run_client; HELP-013. A GDB-handled Ctrl-C is not session cancellation. |
| Bidirectional RTT channel zero and ordinary semihosting console | RTT-001–007, SEMI-001–004. Keep configured RTT port/state; no GDB RSP inspection or semihosting/File-I/O proxy. Transparent OpenOCD/GDB features remain outside guarantees. |
| Clean resources on normal completion, interruption and failure; keep primary failure | Stock finally cleanup plus HELP-010–013, DATA-003. Independent resources get cleanup attempts despite earlier failure. |
| Keep ordinary local runner choice and board-independent integration | SELECT-001–010, INTEG requirements, SCOPE-007. These remain unchanged and outside the session model. |

## B. Necessary remote mechanisms

| Behavior | Product reason/SRS |
| --- | --- |
| SSH, selected executable/fixed options, authentication and deployment | SSH-001–012; HELP-002/003/007; existing SSH capability contract is deliberately smaller than all of OpenSSH. |
| Safe staging/mapping/workspace leases, permissions and removal | FILE-001–008, DATA-002/003/005. Staging operations must not race workspace deletion. |
| Remote loopback/address allocation and collision retries | SVC-003/004; helper supervision and concurrent-session requirements. Retry is an implementation of remote address acquisition, not a user command. |
| Remote generated startup completion and finite determination; required forwards before dependent client | DEBUG-003, SVC-001–006. Required/best-effort classification differs by operation and RTT phase. READY alone is insufficient locally. |
| Observe controller loss concurrently with startup, then bounded process/descendant cleanup | HELP-005/012, §3.5. Detection latency is explicitly outside cleanup's bound. Local detection is **not** remote observation. |
| Separate session, transport, child and cleanup outcomes | HELP-004/009/011. Failed required transport fails the operation; optional forwarding can warn, but cleanup failure is fatal. |
| Local RTT/forwarding lifecycles retain separate owners/failure domains | RTT and SVC requirements. A remote-helper supervisor is not a global owner for these. |

## C. Redesignable internal mechanisms

START/STOP spelling, STOP versus stdin EOF, PROCESS_STARTING as a state event,
ERROR versus SESSION_CLOSED, recognized-prefix queues, pending-control shadows,
admission fences, observer acknowledgements and helper retry states are not
stock-runner behaviors. HELP-006/008 delegate their concrete definition to the
protocol document; the user explicitly permits reconsidering that definition
in this experiment. The SRS itself remains unchanged.

Some *semantic obligations* behind mechanisms survive: stale-effect fencing,
continuous cleanup ownership, final observation before timeout failure,
bounded incremental per-stream relay and validation before action. Their names
and protocol encoding are free to change. Native signal latching and physical
partial acquisition cannot disappear merely because a protocol changes.

Current main's deployment (`remote/deploy.py`) already supplies finite stdin
content and receives DEPLOYED after BOOTSTRAP reads to EOF. The existing
long-lived helper additionally needs streaming input/output while stdin stays
open. Directional EOF/result behavior is therefore an actual current transport
boundary to verify, not merely an optional OpenSSH feature inferred from its name.

The SRS does not require a globally agreed winner between requested close and
natural child exit, or precedence for a parsed-but-unadmitted STOP over remote
READY/retry. It does require preserving an established primary failure. The
product cancellation boundary to evaluate is local refusal of new dependent
work plus cleanup when the remote authority observes controller termination.
Remote cleanup can fail: “no orphan” in the model means continuous ownership
and a successful modeled disposal, not a stronger physical guarantee than
HELP-012 makes when OS termination/reaping fail.

SRS coverage outside the modeled session remains product scope: installation,
per-user setup/configuration discovery, strict YAML validation, environment
allow-listing, generated runner selection/regeneration, path mapping/staging,
concurrent deployments/probe contention, Linux/stdlib/unprivileged operation,
and no persistent artifact cache. They are A integration/functional contracts
or B remote product requirements, not C mechanisms to discard. The full SRS,
especially CONFIG, INTEG/SETUP/SELECT, ENV, FILE, CONC, DATA and NFUNC sections,
remains authoritative for a future implementation.
