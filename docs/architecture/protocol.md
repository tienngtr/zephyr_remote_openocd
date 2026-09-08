# Protocol v1 Helper Contract

This document defines the Protocol v1 helper wire contract. A compatible pair
implements the complete contract, not merely the numeric version.


Protocol v1 is the current change-controlled contract. It uses UTF-8 JSON lines: one JSON object and one `LF` per
frame, with integer, non-Boolean `version: 1` and non-empty string `type`.
Helper stdout contains protocol frames only. Unknown fields are ignored for
compatibility tolerance, but senders MUST NOT use them as an extension mechanism.

Changes require explicit authorization and coordinated updates to this document,
the controller, helper, fixtures, requirements, and compatibility tests. Whether
an authorized change needs a different numeric protocol version is an explicit
compatibility decision; development history alone is not a reason to create one.

The controller first emits `HELLO {helper}` and then
`SESSION_CREATED {session_id, remote_workspace}`; each field is a non-empty
string. Commands travel on controller stdin and events on controller stdout.
There is no feature negotiation beyond the mandatory version.

| Controller command | Required fields | Optional fields and behavior |
| --- | --- | --- |
| `START` | non-empty `services` list; each item has integer, non-Boolean `remote_port` in 1..65535 | Starts the test-only fake service once. Other service-object fields are returned unchanged. |
| `START_OPENOCD` | non-empty `argv`; argv[0] is non-empty and later arguments are strings (empty strings permitted) | `environment` defaults to `{}` and has string keys/values. `required_paths` defaults to `[]` and has `{kind: "file"\|"directory", path: string}`. `services` defaults to `[]` and has `{name: string, remote_port: 1..65535}`. `readiness_marker` is null/absent or a non-empty whitespace-free string. `readiness_timeout` is a positive non-Boolean number, default `30.0`. `literal_prefix` is a non-negative integer no greater than argv length, default `0`; tokens in this prefix are not placeholder-expanded. Starts once. |
| `STOP` | none | Terminates the child process group, removes the workspace, emits `STOPPED {reason: "requested"}`, then exits. |

`START_OPENOCD` expands every `{workspace}` and `{address}` in `argv` and
required-path values, checks required paths, and starts the child in
`<remote_workspace>/staged` with the helper environment overlaid by
`environment`. `PROCESS_STARTED` reports its allocated `127.64.0.0/10` address
and positive PID. With a marker, the helper waits for a complete trimmed line on
either child stream and TCP-connectability of every requested non-GDB service.
GDB is deliberately not probed because OpenOCD can consume its only debugger
connection. It then emits one `SERVICE_READY` per service. Without a marker it
emits no `SERVICE_READY`; a real GDB handshake remains authoritative for GDB.

| Controller event | Required fields | Meaning |
| --- | --- | --- |
| `HELLO` | non-empty string `helper` | First controller event. |
| `SESSION_CREATED` | non-empty strings `session_id`, `remote_workspace` | Second controller event. |
| `PROCESS_STARTED` | non-empty `remote_address`, positive integer `child_pid` | A real OpenOCD child started. |
| `SERVICE_READY` | `remote_address`; either `service` or fake-service `services` plus positive `child_pid` | Individual real readiness or aggregate fake readiness. |
| `CHILD_OUTPUT` | `stream` exactly `stdout`/`stderr`, string `payload` | One child line, UTF-8 decoded with replacement and no trailing `LF`. |
| `PROCESS_EXIT` | integer, non-Boolean `returncode` | Supervised child exited. |
| `STOPPED` | non-empty string `reason` | `requested` after `STOP`; `process-exit` after child exit. |
| `ERROR` | non-empty string `code`, string `message` | Protocol or startup failure; current helper uses `PROTOCOL_ERROR`. |

The valid event sequence is `HELLO`, `SESSION_CREATED`, then fake
`SERVICE_READY` or real `PROCESS_STARTED`. `CHILD_OUTPUT` can race with startup
and occur before `PROCESS_STARTED`; output/readiness may repeat while running.
`PROCESS_EXIT` permits only the following `STOPPED`; `STOPPED` and `ERROR` are
terminal for clients. Malformed JSON, a non-object, invalid/unsupported version,
unexpected command, invalid fields, or invalid state emits `ERROR` and cleans
up. Controller EOF and `SIGINT`/`SIGTERM` also terminate the child process group
and remove the workspace, but promise no final event if it cannot be delivered.

Staging and version probing are separate one-shot helper invocations, not
controller commands. `helper stage <workspace>` reads tar stdin and on success
emits `STAGED {byte_count, sha256, files}`: non-negative byte count, 64-hex
SHA-256, and ordered staged relative paths. `byte_count` is the total number
of extracted regular-file bytes, and `sha256` hashes those bytes concatenated
in the order listed by `files`. The client compares all three confirmations
with its manifest; unsafe content fails the invocation.
`helper openocd-version <command...>` executes exactly
`<command...> --version`; success emits `OPENOCD_VERSION {output}`
with combined output as a string, while failure exits nonzero with `ERROR`.
Deployment bootstrap emits `DEPLOYED {status, path, sha256}`, where status is
`deployed`/`reused`, path is non-empty, and the digest has the same form.

A compatible protocol-v1 pair implements this complete contract, not just the
numeric version. Digest deployment atomically replaces the helper at the
protocol-v1 path with matching source. The configured command prefix is passed
as argv and runner-generated arguments remain separate from it.
Bulk binary content remains stream-oriented instead of JSON/base64.

---
