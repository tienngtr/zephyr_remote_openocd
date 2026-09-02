# Protocol v1 Helper Contract

This document defines the Protocol v1 wire format between the local client and
remote helper. Compatibility requires the complete behavior specified here,
not only a matching version number.

Protocol v1 uses UTF-8 JSON lines: one JSON object followed by one `LF` per
frame. Each frame has an integer, non-Boolean `version: 1` and a non-empty
string `type`.
Helper stdout contains protocol frames only. Unknown fields are ignored for
forward compatibility, but senders MUST NOT use them to add features.

Changes require explicit authorization and coordinated updates to this document,
the client, helper, fixtures, requirements, and compatibility tests. Whether
an authorized change needs a different numeric protocol version is an explicit
compatibility decision; development history alone is not a reason to create one.

The helper first emits `HELLO {helper}` and then
`SESSION_CREATED {session_id, remote_workspace}`; each field is a non-empty
string. The client writes commands to helper stdin, and the helper writes events
to stdout. There is no feature negotiation beyond the required version.

| Client command | Required fields | Optional fields and behavior |
| --- | --- | --- |
| `START` | non-empty `services` list; each item has integer, non-Boolean `remote_port` in 1..65535 | Starts the test-only fake service once. Other service-object fields are returned unchanged. |
| `START_OPENOCD` | non-empty `argv`; `argv[0]` is non-empty and later arguments are strings, including empty strings | Starts OpenOCD once. Optional fields are defined below. |
| `STOP` | none | Terminates the child process group, removes the workspace, emits `STOPPED {reason: "requested"}`, then exits. |

`START_OPENOCD` accepts these optional fields:

- `environment` defaults to `{}` and contains string keys and values.
- `required_paths` defaults to `[]`. Each item has a `kind` of `file` or
  `directory` and a string `path`.
- `services` defaults to `[]`. Each item has a string `name` and an integer,
  non-Boolean `remote_port` from 1 through 65535.
- `readiness_marker` is absent, null, or a non-empty string without whitespace.
- `readiness_timeout` is a positive, non-Boolean number and defaults to `30.0`.
- `literal_prefix` is a non-negative integer no greater than the length of
  `argv`; it defaults to `0`. Placeholder expansion skips this many leading
  arguments.

`START_OPENOCD` expands every `{workspace}` and `{address}` in `argv` and
required-path values, checks required paths, and starts the child in
`<remote_workspace>/staged` with the helper environment overlaid by
`environment`. `PROCESS_STARTED` reports its allocated `127.64.0.0/10` address
and positive PID. With a marker, the helper waits for a complete trimmed line on
either child stream and TCP-connectability of every requested non-GDB service.
GDB is not probed because OpenOCD can consume its only debugger connection. The
helper then emits one `SERVICE_READY` per service. Without a marker, it emits no
`SERVICE_READY`; an actual GDB connection establishes GDB readiness.

| Helper event | Required fields | Meaning |
| --- | --- | --- |
| `HELLO` | non-empty string `helper` | First helper event. |
| `SESSION_CREATED` | non-empty strings `session_id`, `remote_workspace` | Second helper event. |
| `PROCESS_STARTED` | non-empty `remote_address`, positive integer `child_pid` | A real OpenOCD child started. |
| `SERVICE_READY` | `remote_address`; either `service` or fake-service `services` plus positive `child_pid` | Individual real readiness or aggregate fake readiness. |
| `CHILD_OUTPUT` | `stream` exactly `stdout`/`stderr`, string `payload` | One child line, UTF-8 decoded with replacement and no trailing `LF`. |
| `PROCESS_EXIT` | integer, non-Boolean `returncode` | Supervised child exited. |
| `STOPPED` | `reason` is `requested` or `process_exit` | `requested` after `STOP`; `process_exit` after child exit. |
| `ERROR` | non-empty string `code`, string `message` | Protocol or startup failure; current helper uses `PROTOCOL_ERROR`. |

The valid event sequence is `HELLO`, `SESSION_CREATED`, then fake
`SERVICE_READY` or real `PROCESS_STARTED`. `CHILD_OUTPUT` can race with startup
and occur before `PROCESS_STARTED`; output/readiness may repeat while running.
After `PROCESS_EXIT`, only `STOPPED` may follow. `STOPPED` and `ERROR` are
terminal events. Malformed JSON, a non-object, an invalid or unsupported
version, an unexpected command, invalid fields, or an invalid state causes the
helper to emit `ERROR` and clean up. EOF on helper stdin and `SIGINT` or
`SIGTERM` also terminate the child
process group and remove the workspace. A final event is not guaranteed when
the connection cannot deliver it.

Staging and version probing are separate one-shot helper invocations, not
commands in the JSON control protocol. `helper stage <workspace>` reads tar
stdin and on success
emits `STAGED {byte_count, sha256, files}`: non-negative byte count, 64-hex
SHA-256, and ordered staged relative paths. `byte_count` is the total number
of extracted regular-file bytes, and `sha256` hashes those bytes concatenated
in the order listed by `files`. The client compares all three values with its
manifest. Unsafe archive content causes the helper invocation to fail.
`helper openocd-version <command...>` executes exactly
`<command...> --version`; success emits `OPENOCD_VERSION {output}`
with combined output as a string, while failure exits nonzero with `ERROR`.
Deployment bootstrap emits `DEPLOYED {status, path, sha256}`, where status is
`deployed`/`reused`, path is non-empty, and the digest has the same form.

A compatible Protocol v1 client and helper implement this complete contract,
not just the numeric version. Digest deployment atomically replaces the helper
at its version-specific path when the source differs. The configured command
prefix is passed as argv, separate from runner-generated arguments.
Bulk binary content remains stream-oriented instead of JSON/base64.

---
