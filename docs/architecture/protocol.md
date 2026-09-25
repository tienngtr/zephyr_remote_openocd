# Current Remote Helper Contract

This document defines the current internal wire contract between the local
client and remote helper. It retains the numeric wire value `version: 1`; that
value identifies this contract and does not promise compatibility with an
earlier schema. The client and helper are deployed as one revision.

The client validates locally constructed domain models before serializing
commands. Serialization does not re-parse its own output. The helper strictly
validates every command received from the wire, while the client strictly
validates helper events and one-shot responses.

The contract uses UTF-8 JSON lines: each frame contains one JSON object and
ends with one `LF`. JSON whitespace other than `LF` may precede or follow the
object within the frame. Every frame has integer, non-Boolean `version: 1` and
a non-empty string `type`. Persistent commands, persistent events, and
successful one-shot responses contain exactly their documented fields and
reject unknown fields. Helper stdout contains protocol frames only.

The helper emits one `SESSION_CREATED` event before reading commands. The
client writes commands to helper stdin and reads events from stdout. There is
no feature negotiation beyond the required version.

## Persistent commands

`START` is the only process-start command. All of the following fields are
required; no other fields are allowed:

| Field | Value |
| --- | --- |
| `argv` | Non-empty string list; the first string is non-empty and later strings may be empty. |
| `environment` | Object whose names are non-empty strings without `=` or NUL and whose values are strings without NUL. |
| `required_paths` | List of exact `{kind, path}` objects. `kind` is `file` or `directory`; `path` is a non-empty string without NUL. |
| `services` | List of exact `{name, remote_port}` objects. `name` is a non-empty string; `remote_port` is a non-Boolean integer in `1..65535`. Names and ports are unique within the request. |
| `required_output_sentinels` | List of unique non-empty, trimmed output lines without `CR`, `LF`, or NUL; may be empty. |
| `readiness_timeout` | Positive finite, non-Boolean number. |
| `literal_prefix` | Non-Boolean, non-negative integer no greater than the length of `argv`. Placeholder expansion skips this many leading arguments. |

`START` expands `{workspace}` and `{address}` in arguments at and after
`literal_prefix`, and in required-path values. It checks required paths and
starts the child in `<remote_workspace>/staged` with the helper environment
overlaid by `environment`. Service `remote_port` values are unique by
contract, and duplicate values are rejected during validation before startup.

The helper allocates an address in `127.64.0.0/10` and preflights requested
service ports for bind collisions at that address. It does not create or probe
service listeners; OpenOCD owns its GDB, Tcl, telnet, and RTT listeners. With an
empty sentinel list, the process is immediately considered ready. Otherwise
the helper waits until every required sentinel has appeared as a complete
trimmed line on either child stream, then emits one `PROCESS_READY` event.
Sentinels may arrive in any order and on either stream. Output reads are
bounded and use an incremental UTF-8 decoder. A sentinel is recognized only
when the complete trimmed line is observed; a fragment that merely matches a
sentinel prefix does not make the process ready.

`STOP` has no fields other than `version` and `type`. On successful cleanup, it
terminates the child process group, removes the workspace, emits `SESSION_CLOSED`
with `reason: "requested"` and `returncode: null`, and exits. A cleanup failure
may instead end the session with `ERROR`.

`SESSION_CLOSED` is the orderly session-close event. With
`reason: "process_exit"` and an integer `returncode`, it is the sole wire
source of an OpenOCD result. With `reason: "requested"` and
`returncode: null`, it confirms requested shutdown but produces no OpenOCD
result. Natural child termination completes session cleanup, including
workspace removal and output-relay disposal, before emitting
`SESSION_CLOSED` with `reason: "process_exit"`. A cleanup failure may instead
result in `ERROR`. `ERROR` is a failure event that also ends the session.
Neither session-ending event may be followed by another event. The helper's
Unix process status, SSH/control transport status, and forwarding-process
status are independent health observations and are never OpenOCD results.

After local `STOP` initiation, either `SESSION_CLOSED` form may legitimately occur:
OpenOCD may terminate naturally before requested termination takes effect, or
the requested shutdown may complete first. Protocol version 1 is unchanged.

## Persistent events

| Event | Required fields | Meaning |
| --- | --- | --- |
| `SESSION_CREATED` | Non-empty strings `helper`, `session_id`, `remote_workspace` | Session workspace and helper identity are available. |
| `PROCESS_READY` | Non-empty `remote_address`, positive integer `child_pid` | The requested process passed readiness policy. |
| `CHILD_OUTPUT` | `stream` exactly `stdout`/`stderr`, string `payload` without `LF`, Boolean `line_end` | One decoded fragment from the identified child stream. `line_end` is true only when the fragment is followed by an actual child `LF` (the delimiter is omitted). A fragment with `line_end` false has a non-empty payload. UTF-8 decoding is incremental with replacement; one logical line may span several events. |
| `SESSION_CLOSED` | `reason` and `returncode` | Orderly session close: `reason` is `requested` with null return code, or `process_exit` with an integer return code. |
| `ERROR` | Non-empty string `code`, string `message` | Failure event that ends the session. |

The event state graph is:

```text
new --SESSION_CREATED--> created --PROCESS_READY--> active
  \                       |  \                    /
   \                      |   \--SESSION_CLOSED-/
    \                     \------ERROR----------/
     \--ERROR-----------------------------------/
```

`CHILD_OUTPUT` may occur in `created` before `PROCESS_READY` and in `active`.
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

## One-shot operations

Staging, deployment, and version probing are separate helper invocations, not
commands in the persistent control protocol. Each successful invocation emits
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
Duplicate archive paths and file ancestors are rejected before extraction.
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
status and diagnostics rather than a persistent-protocol `ERROR` event.

Bulk binary content remains stream-oriented instead of JSON/base64. The
configured SSH command prefix is passed as argv, separate from runner-generated
arguments.
