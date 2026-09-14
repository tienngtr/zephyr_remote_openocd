# Current Remote Helper Contract

This document defines the current internal wire contract between the local
client and remote helper. It retains the numeric wire value `version: 1`; that
value identifies this contract and does not promise compatibility with an
earlier schema. The client and helper are deployed as one revision.

The contract uses UTF-8 JSON lines: one JSON object followed by one `LF` per
frame. Every frame has integer, non-Boolean `version: 1` and a non-empty string
`type`. Persistent command and event objects reject unknown fields. Helper
stdout contains protocol frames only.

The helper emits one `SESSION_CREATED` event before reading commands. The
client writes commands to helper stdin and reads events from stdout. There is
no feature negotiation beyond the required version.

## Persistent commands

`START` is the only process-start command. All of the following fields are
required; no other fields are allowed:

| Field | Value |
| --- | --- |
| `argv` | Non-empty string list; the first string is non-empty and later strings may be empty. |
| `environment` | Object with valid string names and values. |
| `required_paths` | List of `{kind, path}` objects, where `kind` is `file` or `directory`. |
| `services` | List of `{name, remote_port}` objects. Names and ports are unique within the request. |
| `readiness_marker` | `null` or a non-empty string without whitespace. |
| `readiness_timeout` | Positive finite number. |
| `literal_prefix` | Non-negative integer no greater than the length of `argv`. Placeholder expansion skips this many leading arguments. |

`START` expands `{workspace}` and `{address}` in arguments at and after
`literal_prefix`, and in required-path values. It checks required paths and
starts the child in `<remote_workspace>/staged` with the helper environment
overlaid by `environment`. Service `remote_port` values are unique by
contract, and duplicate values are rejected during validation before startup.

The helper allocates an address in `127.64.0.0/10` and binds requested
non-GDB services there. If `readiness_marker` is `null`, the process is
immediately considered ready. Otherwise the helper waits for a complete
trimmed marker line on either child stream and TCP-connectability of every
requested non-GDB service. GDB is not probed because OpenOCD can consume its
only debugger connection. The helper then emits one `PROCESS_READY` event.

`STOP` has no fields other than `version` and `type`. It terminates the child
process group, removes the workspace, emits `SESSION_CLOSED` with
`reason: "requested"` and `returncode: null`, and exits.

## Persistent events

| Event | Required fields | Meaning |
| --- | --- | --- |
| `SESSION_CREATED` | Non-empty strings `helper`, `session_id`, `remote_workspace` | Session workspace and helper identity are available. |
| `PROCESS_READY` | Non-empty `remote_address`, positive integer `child_pid` | The requested process passed readiness policy. |
| `CHILD_OUTPUT` | `stream` exactly `stdout`/`stderr`, string `payload` | One child line, UTF-8 decoded with replacement and no trailing `LF`. |
| `SESSION_CLOSED` | `reason` and `returncode` | `reason` is `requested` with null return code, or `process_exit` with an integer return code. |
| `ERROR` | Non-empty string `code`, string `message` | Protocol or startup failure. |

The event state graph is:

```text
new --SESSION_CREATED--> created --PROCESS_READY--> active
  \                       |  \                    /
   \                      |   \--SESSION_CLOSED-/
    \                     \------ERROR----------/
     \--ERROR-----------------------------------/
```

`CHILD_OUTPUT` may occur in `created` before `PROCESS_READY` and in `active`.
`SESSION_CLOSED` and `ERROR` are terminal; no event follows either one.
Malformed JSON, a non-object, an invalid version, an unexpected command,
unknown fields, invalid values, or an invalid state causes `ERROR` and cleanup.
EOF on helper stdin and `SIGINT`/`SIGTERM` also terminate the child process
group and remove the workspace. A final event is not guaranteed when the
connection cannot deliver it.

## One-shot operations

Staging, deployment, and version probing are separate helper invocations, not
commands in the persistent control protocol. `helper stage <workspace>` reads
tar stdin and emits `STAGED {byte_count, sha256, files}` on success.
`helper openocd-version <command...>` executes exactly `<command...>
--version` and emits `OPENOCD_VERSION {output}` on success. Deployment emits
`DEPLOYED {status, path, sha256}`; helpers are installed atomically at
`protocol_v1/helper-<sha256>.py`, matching content is reused, and stale digest
revisions are pruned. Deployment serializes installation, reuse refresh, and
pruning with a per-protocol lock so a concurrently selected revision cannot be
removed from a stale observation.

Bulk binary content remains stream-oriented instead of JSON/base64. The
configured SSH command prefix is passed as argv, separate from runner-generated
arguments.
