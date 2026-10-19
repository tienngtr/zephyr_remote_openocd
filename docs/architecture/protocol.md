# Controller-Lease Remote Helper Contract (Protocol v1)

## Compatibility

The client and helper use numeric `version: 1` for session and standalone
operations. Compatibility requires the full contract, not version equality.
There is no negotiation or dual-version session. Content-addressed deployment
supplies matching revisions in the `protocol_v1` helper namespace.

Configured SSH wrappers must deliver directional EOF: closing controller stdin
leaves helper stdout/stderr readable until helper exit. They must also deliver
controller input bytes in order, although not with their local write
boundaries. A wrapper that cannot satisfy this contract requires replacement or
reconfiguration.

Maintained wire, lifecycle, and physical-boundary evidence and selected external
qualification are recorded in
[verification.md](../traceability/verification.md#controller-lease-lifecycle).

## Framing and validation

The envelope is exactly the fields `version` and `type` plus the payload fields
specified below. Each frame is one UTF-8 JSON object followed by LF. JSON
whitespace other than LF may surround the object within its frame. Every frame object contains integer,
non-Boolean `version: 1` and non-empty string `type`. All fields listed for a
frame or nested object are required, including fields whose values may be null.
Unknown fields,
duplicate object keys, invalid types/values, non-finite numbers, and invalid
state transitions are rejected. Integers below always exclude Booleans.
Helper stdout contains protocol frames only. Child stdout/stderr are output
events; helper stderr carries infrastructure diagnostics.

Frames in either direction are limited to 1 MiB (1,048,576 encoded bytes),
including LF. Both complete and incomplete frames are bounded independently,
including when multiple frames arrive in one read. Senders validate domain
objects and encoded size before admission; receivers independently validate
wire data and event order. Decoding, accumulation, diagnostic rendering, and
writer storage must remain bounded. Oversized output is an admission failure,
not permission to exceed the limit or silently discard required outcome detail.

Controller input after SESSION_CREATED has exactly this form:

```text
START [RELEASE] EOF
```

Input is a byte stream; neither local writes nor helper reads are frame
boundaries. A frame may arrive split across reads, and frames may be coalesced
into one read, including START and RELEASE together. Incomplete input remains
buffered, within the frame bound, until its LF or EOF. RELEASE contains exactly
the envelope with `type: RELEASE`. It is valid only after a complete START, at
most once, and only EOF may follow it. Any other frame, a second START or
RELEASE, any byte after RELEASE, or EOF with an incomplete frame is protocol
failure. A complete invalid frame is rejected when recognized, so one batched
with START is rejected before child creation. EOF with no input ends the
session before START without a controller lease or failure.
Already recorded validation/observer failures remain fatal and are accounted
before success publication.

## Session creation and controller lease

The helper allocates and owns a protected workspace, then admits SESSION_CREATED
before reading START. The event contains exactly the envelope and:

| Field | Constraint |
| --- | --- |
| `helper` | Non-empty helper identity string. |
| `session_id` | Non-empty session identity string. |
| `remote_workspace` | Non-empty workspace path string without NUL. |

Creation failure may instead end with SESSION_ENDED without SESSION_CREATED if
output remains usable. No child is started before valid START. Staging uses a
separate helper invocation against the created workspace, before START.

After START, open helper stdin is the controller-lifetime lease. Intentional
local release writes one complete RELEASE frame, then closes stdin; remote stdin
EOF follows while helper stdout/stderr remain usable until helper exit. RELEASE
is causal evidence for the EOF that follows it. It is not itself a lifecycle
transition, termination request, or acknowledged command.

When the helper's control reader recognizes EOF after START, the EOF is orderly
`controller_eof` if that reader recognized a valid RELEASE earlier on the same
input stream. Otherwise it is controller-lease loss: termination with trigger
`controller_eof` and failure `CONTROLLER_LEASE_LOST`. The reader classifies at
recognition and retains a recognized lease loss independently of the helper's
observation queue, before any publication that can block. The lifecycle
authority remains the only transition authority. It accounts the retained loss
before READY admission, at its ordinary accounting points, and immediately
before freezing the terminal snapshot. If another cause already committed
termination, that trigger is kept, and the loss becomes the primary failure if
no failure has been established, including by an earlier natural failing child
result, or a secondary diagnostic otherwise. No input fence, scan for
unread bytes, or acknowledgement is part of this classification: an EOF still
unread when the snapshot freezes is not known to the helper and does not by
itself fail the session.

Remote EOF, native termination signal, natural child exit, protocol failure, or
another fatal failure can initiate termination. Local shutdown must revoke
dependent work, write RELEASE while a started session may still read it,
close stdin, preserve reverse readers for final output/outcome, wait within a
finite budget, and escalate only its owned transport if needed. A session whose
terminal or control end the client has already observed needs no release. A
failed RELEASE write is not itself an operation failure: a still-live helper
then recognizes unreleased EOF and reports lease loss, and transport loss
remains visible through terminal and helper/SSH status accounting. External SSH
sharing masters remain externally owned.

The remote authority handles controller termination independently of the local
operation. EOF occurrence, reader recognition, publication, authority dispatch,
and terminal freeze are distinct stages, not one atomic boundary. READY may
precede recognition of an EOF that the helper has not yet read, and may precede
dispatch of a released EOF. READY is not published after unreleased EOF has
been recognized. Each authority acts on its own observations. After remote
termination commits, no new attempt or READY admission is permitted. Local
cancellation independently revokes local launch eligibility, including for
late READY. The client takes the controller-EOF classification from the
terminal snapshot; when the snapshot is received relative to local closure
never changes it.

## START

START contains exactly the envelope and the following immutable request:

| Field | Value |
| --- | --- |
| `completion_policy` | Exactly `live_server` or `process_exit`; required, with no default or inference from markers/services. |
| `argv` | Non-empty list of strings without NUL; the first string is non-empty and later strings may be empty. |
| `environment` | Object whose names are non-empty strings without `=` or NUL and whose values are strings without NUL. |
| `required_paths` | List of exact `{kind, path}` objects. `kind` is `file` or `directory`; `path` is a non-empty literal string without NUL or an exact `{parts}` path template as defined below. |
| `services` | List of exact `{name, remote_port}` objects. `name` is a non-empty string; `remote_port` is a non-Boolean integer in `1..65535`. Names and ports are unique within the request. |
| `preferred_address` | Null, or a canonical dotted-decimal IPv4 string in `127.64.0.0/10`, excluding `127.64.0.0` and `127.127.255.255`. A hint, not an allocation or lease. |
| `required_output_sentinels` | List of unique non-empty, trimmed startup output markers without `CR`, `LF`, or NUL; may be empty. |
| `readiness_timeout` | Positive finite, non-Boolean number; used only for `live_server`. |
| `literal_prefix` | Non-Boolean, non-negative integer no greater than the length of `argv`. Templates cannot target this many leading arguments. |
| `argv_templates` | List of exact `{index, parts}` objects. Indices are unique non-Boolean integers at least `literal_prefix` and less than the length of `argv`. Each template replaces its indexed argv element. An empty list is valid and required when no templates are used. |

The client selects `completion_policy` from the operation, explicitly:

| Policy | Required behavior |
| --- | --- |
| `live_server` | Used for debug, attach, debugserver, and RTT. READY is required before successful server use or dependent local launch. Child exit before READY is startup failure; the deployed helper does not restart it. |
| `process_exit` | Used for one-shot flash. The helper never admits READY, waits for markers, or starts a readiness timer. Genuine child exit supplies the operation result; zero may succeed only with no operation/infrastructure failure and confirmed required cleanup, and nonzero fails. |

Both policies validate the entire START request. Marker and timeout fields retain
their schema validation but have no readiness effect under `process_exit`.
Empty marker/service lists do not select `process_exit`; non-empty lists do not
select `live_server`. Controller EOF, protocol/output failure, cleanup, and child
result provenance remain applicable to both. Failure to deliver actual required
output is still an infrastructure failure, but `process_exit` cannot fail through
admission of an unnecessary READY.

All ordinary strings are literal, including `{workspace}` and `{address}`
spellings in argv, required paths, mapping destinations, and inherited Tcl.
There is no automatic textual replacement. A template's `parts` is a non-empty
list containing literal strings without NUL and exact `{session}` objects whose
value is `workspace` or `address`. The helper concatenates these parts in order,
inserting its actual workspace or the current attempt's allocated address only
at those explicit references. Inserted values are not scanned for additional
substitutions. An argv element targeted by a template is a planning preview;
the template supplies its complete effective value.

Only argv templates may also contain exact `{tcl_word}` objects. Their value is
a non-empty parts list of literal strings and session references, with no nested
Tcl words. The helper resolves those parts, then quotes the complete result as
one double-quoted Tcl word, backslash-escaping backslashes, double quotes,
`$`, `[`, `]`, `{`, and `}`. Quoting is applied after session values are inserted,
so their literal characters cannot introduce Tcl substitutions. A required-path
template is an exact `{parts}` object containing only literal strings and
session references, and must resolve to a non-empty filesystem path; Tcl word
quoting is invalid there. Unknown fields, invalid parts, repeated indices,
prefix-targeting indices, and out-of-range indices are rejected before startup.

For example, an argv template with
`parts: ["bindto ", {"session": "address"}]` resolves the runner-owned bind
address, while the ordinary string `"echo {address}"` stays literal.
`parts: ["load_image ", {"tcl_word": [{"session": "workspace"}, "/staged/fw.hex"]}]`
resolves and quotes a runner-owned staged firmware path.

The helper materializes templates before child creation, checks required paths,
and starts the child in `<remote_workspace>/staged` with the helper environment
overlaid by `environment`. Service `remote_port` values are unique by
contract, and duplicate values are rejected during validation before startup.

## Attempt admission and startup failure

The helper allocates a leased address in `127.64.0.0/10` and validates requested
service ports for collisions. Before ATTEMPT admission, it tries a non-null
preferred address first, then up to 32 randomized candidates if unavailable;
null uses randomized candidates only. The preference does not bypass leasing or port
validation. Post-spawn bind failure ends the operation. OpenOCD owns
its listeners; the helper does not probe connectability.

After address allocation, template materialization, and required-path
validation, the authority checks current startup state, admits
ATTEMPT with the exact effective argv, establishes physical acquisition ownership,
and invokes spawn. ATTEMPT contains exactly the envelope and:

| Field | Constraint |
| --- | --- |
| `argv` | Non-empty list of strings without NUL; first string non-empty, later strings may be empty. |

ATTEMPT is diagnostic information, not a lifecycle transition or evidence of
successful acquisition. The child creation call is preceded by ATTEMPT,
including failed Popen. Required-path failure emits no ATTEMPT.
Failed ATTEMPT admission prevents spawn. Admission means local bounded writer
acceptance, not a completed pipe write or peer receipt.

The helper attempts child creation at most once per START. Opaque/user Tcl
and target initialization may precede a bind failure, so neither output
diagnostics nor a genuine bind collision authorize replay. Pre-spawn
address-candidate selection applies to both completion policies.
Cancellation and timeout do not establish producer or resource settlement.
See SAD §38 for ownership and startup-failure policy.

## READY

READY contains exactly the envelope and:

| Field | Constraint |
| --- | --- |
| `remote_address` | Canonical dotted-decimal IPv4 in `127.64.0.0/10`, excluding its first and last addresses. |
| `child_pid` | Positive integer identifying the owned child leader. |

READY is permitted only for `live_server`. It requires current startup
state, an owned child, all requested startup evidence, a final
live-child validation, and successful protocol admission. Only then can the
remote authority activate the attempt. Admission failure terminates the
session. At most one READY is admitted per session.
READY alone never authorizes GDB or RTT launch: the local operation also
validates required forwarding, cancellation, and established failures at
actual execution entry.

Required markers are complete trimmed lines on either stream, matched exactly
and in any order using incremental UTF-8 decoding with replacement. Prefix
fragments do not count. An actual stream EOF finalizes the decoder and its
last unterminated line, which can satisfy a complete marker. For `live_server`
with no markers, startup evidence is immediately satisfied but
ownership/live-child/admission checks still apply. `process_exit` never admits
READY, regardless of its marker list; its result comes from SESSION_ENDED.

For `live_server`, the readiness deadline starts a bounded final observation
of retained decoder evidence, finite available stream bytes, actual EOF, and
child exit. SAD §40 defines the finite scan policy. Then readiness or startup
failure is chosen; continued output cannot indefinitely extend determination.
A dead child cannot authorize live READY. Controller EOF and deadline handling
may race without fabricating child status or restoring local launch
eligibility.

## CHILD_OUTPUT

CHILD_OUTPUT contains exactly the envelope and:

| Field | Constraint |
| --- | --- |
| `stream` | Exactly `stdout` or `stderr`. |
| `payload` | String without LF. |
| `line_end` | Boolean; true only for an actual child LF following the fragment, with that LF omitted. A false value requires non-empty payload. |

A logical line may span several events. Newline-free fragments remain observable
before child exit; UTF-8 decoding uses replacement for invalid bytes. Actual
EOF flushes the decoder without inventing a newline. Markers
inspect retained decoding evidence without requiring successful
bulk-output delivery.

Output is allowed during startup, activity, and termination, after its ATTEMPT
and before SESSION_ENDED. Order is preserved within each stream. There is no
total stdout/stderr write ordering promise. Bulk backpressure cannot block controller/signal
observation, child exit accounting, or resource cleanup. Final retained output
receives a finite drain opportunity under the bounded SAD policy; no event can
follow the terminal frame.

## SESSION_ENDED and structured outcome

SESSION_ENDED is the only session terminal event, for success and failure alike.
It contains exactly the envelope and:

| Field | Constraint |
| --- | --- |
| `trigger` | One of `controller_eof`, `signal`, `child_exit`, `startup_failure`, `protocol_failure`, `output_failure`, `helper_failure`; identifies the initiating cause, not a child result. |
| `primary_failure` | Null or a Diagnostic object below. |
| `diagnostics` | Ordered list of Diagnostic objects for secondary or informational details. |
| `child_result` | Null or exactly `{returncode, termination_requested}`: returncode is an observed integer child status, including negative signal status; termination_requested is Boolean as defined below. |
| `cleanup` | Exactly the Cleanup object below. |

A Diagnostic is exactly `{code, message, diagnostics}`. `code` is a non-empty
string, `message` is a string, and `diagnostics` is an ordered list of nested
Diagnostics, possibly empty. Nesting retains cleanup-batch and secondary-exception
detail without using exception notes as the canonical outcome. These trees and
their encoding remain subject to the frame and bounded-memory limits.

Cleanup is exactly:

| Field | Constraint |
| --- | --- |
| `child_disposal` | `not_acquired`, `confirmed`, or `unconfirmed`. Covers attempt acquisition producer, address lease, process group, leader reaping, and owned child descriptors/relays. `not_acquired` requires no child acquisition and no remaining attempt-resource obligation. |
| `workspace_disposal` | `not_created`, `confirmed`, or `unconfirmed`. `confirmed` requires safe removal of workspace and per-session coordination artifacts. |
| `residual_resources` | List of unique names drawn from `child_producer`, `child_group`, `child_relays`, `address_lease`, `workspace`, `workspace_metadata`, identifying outstanding disposal obligations. |

A failure trigger (`signal`, `startup_failure`, `protocol_failure`,
`output_failure`, or `helper_failure`) requires a non-null primary failure.
Controller EOF or child exit can also coexist with a primary failure established
earlier or during cleanup. A `child_exit` trigger requires a non-null genuine
child result.
`not_acquired`/`not_created` exclude corresponding residuals,
and a non-null child result is incompatible with `child_disposal: not_acquired`.
Disposal claims must also agree with earlier session events: SESSION_CREATED
rules out `workspace_disposal: not_created`, while READY or CHILD_OUTPUT rules
out `child_disposal: not_acquired`. ATTEMPT alone does not prove child acquisition.
Creation can fail after acquiring a workspace but before admitting SESSION_CREATED,
so a terminal-only history may still report confirmed workspace disposal.

A native SIGINT/SIGTERM received by the helper is a helper/session failure,
under either completion policy. When the authority accounts it, the signal
failure becomes primary if none exists; otherwise its diagnostic remains
secondary and the established primary is preserved. If it initiates termination,
`trigger: signal` therefore cannot accompany `primary_failure: null`, even when
disposal is confirmed, the child result is zero, and helper/SSH exits zero.
The client rejects that invalid snapshot; a valid signal-triggered snapshot
fails the operation through its non-null primary failure.

If controller EOF or another cause already initiated termination, the signal
still records failure without changing that trigger or abandoning cleanup.
If the snapshot has already frozen, retain the signal failure in local
diagnostics and return nonzero helper status; do not mutate or replace the
snapshot. Independent helper-status validation makes that late failure visible.
Intentional local closure uses RELEASE followed by controller EOF. Signals sent by the helper to its
owned child group for disposal are child cleanup, not native signals received
by the helper, and do not by themselves establish this helper/session failure.

Unconfirmed disposal requires a corresponding residual resource and a primary
or secondary cleanup-failure diagnostic. Confirmed child disposal excludes child
and address-lease residuals; confirmed workspace disposal excludes workspace residuals. An
unconfirmed child/producer that may use staged inputs also requires unconfirmed
workspace disposal and retained inputs. Leader exit, successful signal delivery,
cancellation, or cleanup wait expiry cannot confirm the entire child scope.
Cleanup must close new staging admission, attempt independent process cleanup,
and remove workspace only after dependent disposal and exclusive staging
settlement. Failed disposal can leave the helper closed with residual obligations.

The initiating trigger is fixed when remote termination is committed. Once a
primary failure is established, later cleanup, observer, transport, or writer
failures remain ordered secondary diagnostics. No total ordering is promised
between unrelated failures before one is established. An independently observed
nonzero child result fails a one-shot operation, but result provenance stays
independent of whether it becomes the primary failure. A natural nonzero child
result observed before any failure is itself the established operation failure:
later failures, such as cleanup failure or controller-lease loss, become
secondary diagnostics and `primary_failure` stays null. A failure established
first stays primary when a natural nonzero result is observed afterward. A zero
result cannot mask infrastructure or cleanup failure.

`termination_requested` records whether the helper issued a cleanup termination
signal to the child scope before observing its status. Before signalling, account
any already observable child exit. This flag records the local observation
context, not proof of which physical cause made the child exit. It is independent
of the initiating trigger: EOF-triggered shutdown may still observe a natural
exit before signalling. Normal server disposal after successful local client
work does not fail that work solely due to an observed nonzero cleanup status.
An independently observed natural nonzero result remains operation failure;
one-shot flash requires genuine completion rather than a shutdown-induced result.

The terminal child result originates only from observing the session's owned
child, including observation during requested termination. EOF-triggered closure
can therefore include a genuine child result. Helper, SSH, forwarding, timeout,
and cleanup statuses cannot be synthesized into it. No child observation means
null, even with zero helper/SSH status. A failed spawn can have an ATTEMPT and
still have null child result. Signal failure is retained as a primary or
secondary Diagnostic; it never substitutes a signal number for child status.

Freeze at most one immutable snapshot after cleanup attempts and retained-output
accounting. SESSION_ENDED is admitted after the output retained for delivery.
Failed admission, partial writes, or transport loss do not allow another terminal
snapshot or replay from its beginning. Local diagnostics may grow after freezing
but cannot mutate the wire snapshot. The output writer gets a finite drain
opportunity and no other session event follows SESSION_ENDED. Terminal delivery
is not guaranteed after output/transport failure.

The client records remote outcome, disposal confirmation, and helper/SSH status
independently. Missing or malformed terminal information is infrastructure
uncertainty, not success or confirmed remote disposal. Nonzero helper/SSH status
after a valid snapshot remains independent failure evidence. A valid snapshot
with primary failure or unconfirmed required disposal fails the operation. Later
cleanup cannot replace an already established local operation failure.

## Allowed session histories and failure boundaries

```text
helper: SESSION_CREATED
client: START; stdin remains open as lease
helper: ATTEMPT?; CHILD_OUTPUT*; READY?; CHILD_OUTPUT*; SESSION_ENDED
client: RELEASE then EOF at intentional closure; input EOF alone may occur at any point
```

The compact grammar is illustrative: setup/startup failure can end before
ATTEMPT or READY. SESSION_ENDED may be the first and only frame if creation
failed. The client rejects duplicate creation or ATTEMPT, READY before
ATTEMPT, READY under `process_exit`, output or child result without ATTEMPT,
duplicate terminal events, and any event after terminal. A successful
`live_server` startup requires READY; pre-readiness failure/cancellation may end without it.
Under `process_exit`, READY is forbidden and genuine process completion
requires no readiness event. Failed spawn remains an admitted diagnostic
attempt with no acquired process.

Malformed JSON/UTF-8, non-object input, invalid version/schema, oversize input,
post-START input other than one RELEASE, bytes after RELEASE, or invalid state becomes protocol
failure when handled by the authority and initiates cleanup. Failed session
creation, START validation, spawn, readiness, or output admission ends through the same outcome model if a
terminal frame can be delivered. Unexpected transport loss is local failure;
remote cleanup begins after remote authority observation of EOF/signal, and an
unreleased EOF the helper recognizes is controller-lease loss.

Intentional benign races include late remote READY after local cancellation,
READY before the helper recognizes or dispatches controller EOF as allowed
above, and natural exit versus controller termination selecting either
initiating trigger. They are permitted only while
local launch gating, single-attempt admission, result provenance, continuous
cleanup reachability, and eventual bounded cleanup after authority observation
remain satisfied. Local and remote authorities observe controller loss
independently.
Already recorded fatal physical failures are accounted before success
publication; they remain failures even if ordinary queue dispatch is pending.
This local check does not wait for future controller input.

## Standalone helper operations

Staging, deployment, and version probing are separate helper invocations, not
commands in the session control protocol. Each successful invocation emits
exactly one response frame on stdout, with no additional output. Each response
contains only the envelope and fields listed below:

| Response | Fields and constraints |
| --- | --- |
| `STAGED` | `byte_count` is a non-Boolean integer greater than or equal to zero; `sha256` is a 64-character lowercase hexadecimal digest; `files` and `directories` are lists of normalized relative POSIX paths. |
| `OPENOCD_VERSION` | `output` is a string. |
| `DEPLOYED` | `status` is exactly `deployed` or `reused`; `path` is a non-empty string; `sha256` is a 64-character lowercase hexadecimal digest. |

A normalized manifest path is a non-empty relative string without NUL, empty,
`.` or `..` components. Each of `files` and `directories` contains unique
paths, the lists are disjoint, and no listed file is an ancestor of another
listed path. Directory entries are explicit. `byte_count` and `sha256` cover
regular-file content only. Both manifest lists preserve archive encounter order
within their respective kind. `byte_count` is the sum of regular-file byte
lengths, and `sha256` is the SHA-256 digest of regular-file bytes concatenated
in archive encounter order, without paths, metadata, directory entries, or
boundary bytes.

`helper stage <workspace>` reads tar stdin and emits `STAGED` on success.
The destination is `<workspace>/staged` within an active helper session.
The helper spools the upload and validates the complete member list before
extracting any member. Archive members must satisfy all of these rules:

- Only directories and regular files are accepted. Symlinks, hard links,
  devices, FIFOs, and other special member types are rejected. Directory members
  must have zero content size.
- Decoded member names must be normalized relative POSIX paths as defined above.
  One trailing slash on a directory name is removed before validation and
  manifest reporting. Absolute or empty names, NUL, empty components, `.` or
  `..` components, and other non-normalized spellings are rejected.
- Normalized member paths must be unique across both kinds. A regular-file path
  must not be an ancestor of any other member, regardless of encounter order.
- Every resolved destination must remain strictly below the resolved staging
  root. A destination that resolves outside it or to the root itself is rejected.

An invalid member rejects the archive before any member is extracted. During
extraction, implicit parent directories are created as needed beneath the
staging root using normal OS permission and umask handling. Explicit directory
entries are forced to mode `0700`, regardless of archive permissions. Regular
files receive `member.mode & 0700`, or `0600` if that mask is zero. Group/other
permissions and special mode bits are discarded; archive ownership, timestamps,
and other filesystem metadata are not applied.

The client accepts staging only after a successful invocation and exactly one
valid `STAGED` response. Its ordered `files` and `directories` lists must match
the local archive's respective manifests, and `byte_count` and `sha256` must
match the locally computed values using the content rules above. Missing,
malformed, or mismatched confirmation fails staging; the session must not start
OpenOCD and instead attempts session cleanup.

`helper openocd-version <command...>` executes exactly `<command...>
--version` and emits `OPENOCD_VERSION` on success.

The deployment bootstrap emits `DEPLOYED` on success. Helpers are installed
atomically at `protocol_v1/helper-<sha256>.py`, and matching content is reused.
Stale digest revisions older than 24 hours are eligible for opportunistic
reclamation, excluding the selected revision. The bootstrap attempts each
eligible removal and tolerates an individual stat or removal failure; such a
failure does not fail deployment, and the selected revision is never removed.
Deployment serializes installation, reuse refresh, and reclamation with a
per-protocol lock so a concurrently selected revision cannot be removed from a
stale observation.

`ERROR` is a standalone failure response containing exactly the envelope,
non-empty string `code`, and string `message`. It is not a session event.

After a valid `stage` or `openocd-version` helper invocation has been selected,
an operation failure emits one `ERROR` frame and exits nonzero. Invocation
parsing, SSH, or transport failure may instead terminate without a usable
response. Deployment bootstrap failure is reported by a nonzero subprocess
status and diagnostics rather than a session-protocol `ERROR` event.

Bulk binary content remains stream-oriented instead of JSON/base64. The
configured SSH command prefix is passed as argv, separate from runner-generated
arguments.
