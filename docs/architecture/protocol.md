# Current Remote Helper Contract

This document defines the current internal wire contract between the local
client and remote helper. It retains the numeric wire value `version: 1`; that
value identifies this contract and does not promise compatibility with an
earlier schema. The client and helper are deployed as one revision.

The client validates locally constructed domain models before serializing
commands. Serialization does not re-parse its own output. The helper strictly
validates every command received from the wire, while the client strictly
validates helper events and standalone responses.

The contract uses UTF-8 JSON lines: each frame contains one JSON object and
ends with one `LF`. JSON whitespace other than `LF` may precede or follow the
object within the frame. Every frame has integer, non-Boolean `version: 1` and
a non-empty string `type`. Session commands, session events, and successful
standalone responses contain exactly their documented fields and reject unknown
fields. Helper stdout contains protocol frames only.

Control frames on helper stdin are limited to 1 MiB (1,048,576 bytes), including
the LF delimiter. The bound applies to each frame independently, including
when multiple frames arrive together. An oversized complete or incomplete
frame is a protocol error. This bounded-input requirement retains Protocol v1
message shapes and version; callers must keep the complete encoded `START`
frame within this bound.
The client checks the encoded byte count, including LF, before writing any
command bytes. The helper independently enforces the same bound on incoming
complete and partial frames.

The helper emits one `SESSION_CREATED` event before reading commands. The
client writes commands to helper stdin and reads events from stdout. There is
no feature negotiation. Compatibility requires the complete current contract,
including explicit argument templates and the pre-spawn `PROCESS_STARTING`
event and the required nullable `preferred_address` field, not version equality
alone. Clients omitting that field and helpers rejecting it are incompatible
with the current contract. Earlier version-1 clients or helpers using
automatic textual placeholder expansion are incompatible: `START` now requires
`argv_templates`, even when empty. Normal deployment installs the matching
content-addressed helper automatically;
no user configuration migration is required and the numeric version remains 1.

## Session commands

`START` is the only process-start command. All of the following fields are
required; no other fields are allowed:

| Field | Value |
| --- | --- |
| `argv` | Non-empty string list; the first string is non-empty and later strings may be empty. |
| `environment` | Object whose names are non-empty strings without `=` or NUL and whose values are strings without NUL. |
| `required_paths` | List of exact `{kind, path}` objects. `kind` is `file` or `directory`; `path` is a non-empty literal string without NUL or an exact `{parts}` path template as defined below. |
| `services` | List of exact `{name, remote_port}` objects. `name` is a non-empty string; `remote_port` is a non-Boolean integer in `1..65535`. Names and ports are unique within the request. |
| `preferred_address` | Null, or a canonical dotted-decimal IPv4 string in `127.64.0.0/10`, excluding `127.64.0.0` and `127.127.255.255`. A hint, not an allocation or lease. |
| `required_output_sentinels` | List of unique non-empty, trimmed startup output markers without `CR`, `LF`, or NUL; may be empty. |
| `readiness_timeout` | Positive finite, non-Boolean number. |
| `literal_prefix` | Non-Boolean, non-negative integer no greater than the length of `argv`. Templates cannot target this many leading arguments. |
| `argv_templates` | List of exact `{index, parts}` objects. Indices are unique non-Boolean integers at least `literal_prefix` and less than the length of `argv`. Each template replaces its indexed argv element. An empty list is valid and required when no templates are used. |

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

The helper materializes templates for each spawn attempt, checks required paths,
and starts the child in `<remote_workspace>/staged` with the helper environment
overlaid by `environment`. Service `remote_port` values are unique by
contract, and duplicate values are rejected during validation before startup.

The helper allocates an address in `127.64.0.0/10` and checks requested service
ports for bind collisions at that address. On the initial child attempt it
tries a non-null preferred address first, using the same cross-session address
lease and complete service-port validation as randomized candidates. An
unavailable preference falls back to up to 32 randomized candidates; null uses
only randomized candidates. A child bind-collision retry uses randomized
allocation, without trying the preference again. The helper-selected address
is reported in `PROCESS_READY`; the preference never bypasses leasing or port
validation. It does not create or probe service
listeners; OpenOCD owns its GDB, Tcl, telnet, and RTT listeners. After allocating
the address, materializing the argv, and validating required paths, the helper
emits `PROCESS_STARTING` with the exact argv immediately before attempting to
spawn the child. A required-path failure emits no `PROCESS_STARTING` event.
Each bind-collision retry emits its own event with that attempt's
resolved values. Literal strings remain unchanged across retries. This event
does not indicate successful spawning or readiness
and remains observable when spawning or readiness subsequently fails.

With an empty marker list, the process is immediately startup-ready after
spawning. Otherwise the helper waits until every required marker has appeared
as a complete trimmed line on either child stream, then emits one
`PROCESS_READY` event. Markers may arrive
in any order and on either stream. Output reads are bounded and use an
incremental UTF-8 decoder. A marker is recognized only when the complete
trimmed line is observed; a fragment that merely matches a marker prefix does
not make the process startup-ready.

While startup readiness is pending, the helper continues consuming control
frames. `STOP` and stdin EOF end the session without waiting for startup
readiness or emitting `PROCESS_READY`; malformed or unexpected commands cause
protocol failure and cleanup. Incomplete frames remain buffered until their LF
arrives, and EOF with an incomplete frame is a protocol error.

A bind-collision retry cannot emit another `PROCESS_STARTING` or spawn another
child after STOP, EOF, or a control-protocol failure has become an observed
pending fact during old-attempt cleanup. Before committing the retry, the
coordinator accounts for consumed control facts even when their publication is
blocked by the bounded observation queue. The retry boundary also accounts for
latched termination signals and cleanup/observer failures. It does not wait for
future input or promise to detect bytes that have not yet been observed. This
is lifecycle ordering within the existing wire contract, not a new frame type.

`STOP` has no fields other than `version` and `type`. On successful cleanup, it
terminates the child process group, removes the workspace, emits `SESSION_CLOSED`
with `reason: "requested"` and `returncode: null`, and exits. A cleanup failure
may instead end the session with `ERROR`.

`SESSION_CLOSED` is the orderly session-close event. With
`reason: "process_exit"` and an integer `returncode`, it is the sole wire
source of an OpenOCD result. With `reason: "requested"` and
`returncode: null`, it confirms requested shutdown but produces no OpenOCD
result. Natural child termination completes session cleanup, including
workspace removal and output-relay cleanup, before emitting
`SESSION_CLOSED` with `reason: "process_exit"`. A cleanup failure may instead
result in `ERROR`. `ERROR` is a failure event that also ends the session.
Neither session-ending event may be followed by another event. The helper's
Unix process status, SSH/control transport status, and forwarding-process
status are independent status checks and are never OpenOCD results.

After local `STOP` initiation, either `SESSION_CLOSED` form may legitimately occur:
OpenOCD may terminate naturally before requested termination takes effect, or
the requested shutdown may complete first. The numeric protocol version remains 1.

## Session events

| Event | Required fields | Meaning |
| --- | --- | --- |
| `SESSION_CREATED` | Non-empty strings `helper`, `session_id`, `remote_workspace` | Session workspace and helper identity are available. |
| `PROCESS_STARTING` | `argv`: non-empty string list; first string non-empty, later strings may be empty | Required paths validated; exact materialized argv for one child attempt, emitted immediately before spawn. Repeated for retries before readiness. |
| `PROCESS_READY` | Non-empty `remote_address`, positive integer `child_pid` | The requested process passed readiness policy. |
| `CHILD_OUTPUT` | `stream` exactly `stdout`/`stderr`, string `payload` without `LF`, Boolean `line_end` | One decoded fragment from the identified child stream. `line_end` is true only when the fragment is followed by an actual child `LF` (the delimiter is omitted). A fragment with `line_end` false has a non-empty payload. UTF-8 decoding is incremental with replacement; one logical line may span several events. |
| `SESSION_CLOSED` | `reason` and `returncode` | Orderly session close: `reason` is `requested` with null return code, or `process_exit` with an integer return code. |
| `ERROR` | Non-empty string `code`, string `message` | Failure event that ends the session. |

The event state graph is:

```text
new --SESSION_CREATED--> created --PROCESS_STARTING--> starting --PROCESS_READY--> active
                                                        |
                                                        +--PROCESS_STARTING--> starting

created, starting, active --SESSION_CLOSED--> closed
new, created, starting, active --ERROR--> closed
```

`CHILD_OUTPUT` may occur in `starting` before `PROCESS_READY` and in `active`.
The client rejects child output or readiness before `PROCESS_STARTING`, and
rejects `PROCESS_STARTING` after readiness or any session-ending event. It
delivers each reported argv to the process-start observer while awaiting
readiness, before required forwarding or dependent client startup. The observer
does not reconstruct template materialization locally.
Fragment order is preserved within each child stream. Events from stdout and
stderr are serialized in helper-observed order; no ordering relationship
between writes to different child streams is guaranteed.
`SESSION_CLOSED` follows relay completion. `SESSION_CLOSED` and `ERROR` both
are session-ending events, and no event follows either one.
Malformed JSON, a non-object, an invalid version, an unexpected command,
unknown fields, invalid values, or an invalid state causes `ERROR` and cleanup.
EOF on helper stdin and `SIGINT`/`SIGTERM` also terminate the child process
group and remove the workspace. A final event is not guaranteed when the
connection cannot deliver it.

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
extraction, implicit parent directories are created with mode `0700`, and
explicit directory entries are forced to mode `0700`, regardless of archive
permissions. Regular files receive `member.mode & 0700`, or `0600` if that mask
is zero. Group/other permissions and special mode bits are discarded; archive
ownership, timestamps, and other filesystem metadata are not applied.

The client accepts staging only after a successful invocation and exactly one
valid `STAGED` response. Its ordered `files` and `directories` lists must match
the local archive's respective manifests, and `byte_count` and `sha256` must
match the locally computed values using the content rules above. Missing,
malformed, or mismatched confirmation fails staging; the session must not start
OpenOCD and instead attempts session cleanup.

`helper openocd-version <command...>` executes exactly `<command...>
--version` and emits `OPENOCD_VERSION` on success.

The deployment bootstrap emits `DEPLOYED` on success. Helpers are installed
atomically at `protocol_v1/helper-<sha256>.py`, matching content is reused, and
stale digest revisions are pruned. Deployment serializes installation, reuse
refresh, and pruning with a per-protocol lock so a concurrently selected
revision cannot be removed from a stale observation.

After a valid `stage` or `openocd-version` helper invocation has been selected,
an operation failure emits one `ERROR` frame and exits nonzero. Invocation
parsing, SSH, or transport failure may instead terminate without a usable
response. Deployment bootstrap failure is reported by a nonzero subprocess
status and diagnostics rather than a session-protocol `ERROR` event.

Bulk binary content remains stream-oriented instead of JSON/base64. The
configured SSH command prefix is passed as argv, separate from runner-generated
arguments.
