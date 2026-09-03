# Zephyr Remote OpenOCD Custom Runner
## Software Architecture Document
### V1 — Draft 0.5

# 1. Purpose

This document describes the V1 architecture for the Zephyr Remote OpenOCD Custom Runner.

The SRS defines externally required behavior.

This document describes implementation structure and architectural decisions.

---

# 2. Architecture Drivers

The primary drivers are:

- no development-repository changes;
- board-agnostic design;
- native Linux and WSL 2 as equal V1 platforms;
- Zephyr 4.4 compatibility;
- local GDB and remote OpenOCD;
- reuse of existing board OpenOCD configuration;
- concurrent remote sessions;
- user-selectable SSH client;
- minimal remote administration;
- maintainable Zephyr-version isolation.

---

# 3. Architecture Overview

```text
                   LOCAL HOST
             Linux or WSL 2

               west command
                    |
                    v
               Zephyr west
                    |
          structured runner state
                    |
                    v
        RemoteOpenOcdBinaryRunner
          /         |          \
         /          |           \
    staging      session       local clients
                 manager       GDB / RTT
         \          |
          \         v
           +----------------+
            SSH abstraction
                    |
           configured client
             ssh / ssh.exe
                    |
====================|====================
                    |
                    v
                REMOTE HOST

               remote helper
                    |
        +-----------+-----------+
        |           |           |
     staging    loopback    supervision
                allocation
                    |
                    v
                 OpenOCD
                    |
               debug probe
                    |
                  target
```

No component above the normal OpenOCD configuration layer is board-specific.

---

# 4. Supported Local Platforms

Native Linux and WSL 2 are both V1 development platforms.

The generic Python implementation shall not branch into separate Linux and WSL product architectures.

Platform-specific handling should be confined to narrow adapter points such as:

- command discovery;
- subprocess invocation;
- path representation when interacting with an external Windows executable.

WSL 2 is not treated as a compatibility port of a Linux-only design.

---

# 5. Self-Contained Zephyr Module

V1 is distributed as a self-contained Zephyr module rather than an installed Python distribution.

Current implementation structure:

```text
zephyr-remote-openocd/
    zephyr/
        module.yml
        CMakeLists.txt

    runners/
        remote_openocd.py

    python/
        zephyr_remote_openocd/
            __init__.py
            config.py

            zephyr44/
                runner.py

            remote/
                model.py, paths.py, staging.py, ssh.py
                services.py, session.py, protocol.py
                backend.py, deploy.py, debug.py, flash.py, rtt.py

            remote_helper.py

    resources/
        config.toml.example

    scripts/
        config_default.py
        setup.py
```

The implementation is intentionally self-contained in the module tree. Exact
filenames are not architectural contracts. User setup is implemented by
`scripts/setup.py`; pip packaging is not required for V1.

---

# 6. Python Import Model

Zephyr loads the external runner entry point from the module.

The entry point only bootstraps the module's own Python tree.

Conceptually:

```python
from pathlib import Path
import sys

module_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(module_root / "python"))

from zephyr_remote_openocd.zephyr44.runner import (
    RemoteOpenOcdBinaryRunner,
)
```

The substantive implementation remains split into normal Python modules.

The local runner may use `pyelftools` for ELF inspection, consistent with
Zephyr 4.4's built-in OpenOCD runner. It is an accepted V1 runtime dependency,
not functionality to reimplement. Setup and diagnostics should verify that
`elftools` is importable and report that the supported Zephyr Python environment
is expected to provide it when it is missing. The module itself still does not
require pip packaging or a separate bespoke installation path.

---

# 7. Distribution and User Setup

Distribution places the complete module at an arbitrary persistent path. A
convenient documented example is:

```text
~/zephyrproject/zephyr-remote-openocd
```

This path is only an example; the module may live anywhere persistent.

User setup is a separate, non-invasive operation:

```text
python3 scripts/setup.py
```

The setup script copies `resources/config.toml.example` only when the canonical
per-user configuration is absent, reports the created/reused status and both
absolute paths, and prints guidance for `EXTRA_ZEPHYR_MODULES`. It creates the
`zephyr-remote-openocd` configuration directory with mode `0700` and the file
with mode `0600`; existing parents, directories, and files are never chmodded.
It does not edit shell startup files, repositories, or `.zephyrrc`.

---

# 8. Configuration Template

The canonical V1 template is `resources/config.toml.example`:

```toml
# Zephyr Remote OpenOCD V1 configuration
#
# This file lists every supported V1 key. Unknown keys are rejected so that
# spelling mistakes and unsupported settings fail with an actionable error.

[zephyr]
# Default runner for OpenOCD-capable builds: "local" or "remote".
default = "local"

[remote]
# OpenSSH host or alias. Required when remote-openocd is used.
# host = "openocd-host"

# Normalized absolute POSIX path to OpenOCD on the remote Linux host.
# Required when remote-openocd is used.
# openocd = "/absolute/path/to/openocd"

[ssh]
# Non-empty OpenSSH-compatible command argv. Fixed arguments are allowed.
command = ["ssh"]
# command = ["ssh", "-F", "/home/user/.ssh/lab_config"]
# WSL 2 may select Windows OpenSSH explicitly:
# command = ["/mnt/c/Windows/System32/OpenSSH/ssh.exe"]

[openocd]
# Unique local environment-variable names forwarded when present.
forward_env = []

# Zero or more recursive local-to-remote path mappings. Local paths must be
# absolute after optional '~' expansion. Remote paths must be normalized,
# absolute POSIX paths. The longest matching local prefix wins.
# [[paths.map]]
# local = "/home/user/openocd/share/openocd/scripts"
# remote = "/opt/openocd/share/openocd/scripts"
```

The parser rejects all unknown keys. Optional remote settings may be absent
while the local runner is selected, but both are required by every production
`remote-openocd` operation. Local mapping paths are expanded and resolved once
when loading configuration; remote paths are validated lexically because they
belong to the remote Linux filesystem. Exact duplicate mappings and conflicting
mappings for the same normalized local path are both errors.

This enumerated schema is frozen for V1. Field additions, removals, renames, type
changes, or validation changes require an explicit compatibility and migration
decision rather than silent extension of the V1 schema.

The SSH command is represented as an argv list rather than a shell command string.

Rationale:

An argv representation:

- avoids shell quoting ambiguity;
- permits fixed arguments;
- avoids unnecessary shell invocation;
- works naturally with Python `subprocess`;
- supports both Linux executables and Windows executables invoked from WSL.

This remains analogous in purpose to `GIT_SSH_COMMAND` without requiring shell-string semantics.

---

# 9. Module Discovery

The module is supplied through:

```text
EXTRA_ZEPHYR_MODULES=<module-root>
```

The project does not prescribe how the user stores this setting.

Documentation shall show at least one convenient repository-independent approach.

---

# 10. External Runner Registration

`zephyr/module.yml` declares:

```yaml
name: zephyr-remote-openocd

runners:
  - file: runners/remote_openocd.py
```

The custom Python runner reports:

```text
remote-openocd
```

Python discovery is complemented by CMake integration because west also validates runner availability from generated build runner state.

---

# 11. Conditional Build-Time Runner Augmentation

For a build with:

```text
openocd
```

the module adds:

```text
remote-openocd
```

and mirrors applicable built-in OpenOCD runner arguments.

For a build without `openocd`, the module does not add `remote-openocd`.

No board name, vendor name, architecture, or SoC family participates in this decision.

Eligibility is based exclusively on existing OpenOCD runner support.

---

# 12. Common Runner Configuration

Common `RunnerConfig` data is reused directly.

Typical fields include:

```text
board_dir
elf_file
hex_file
bin_file
gdb
openocd_search
```

The remote runner does not duplicate these fields under board-specific configuration.

---

# 13. Runner-Specific Argument Mirroring

The CMake compatibility layer mirrors applicable arguments associated with:

```text
openocd
```

to:

```text
remote-openocd
```

Representative result:

```yaml
args:
  openocd:
    - --cmd-load
    - flash write_image erase
    - --cmd-verify
    - verify_image
    - --file-type=elf

  remote-openocd:
    - --cmd-load
    - flash write_image erase
    - --cmd-verify
    - verify_image
    - --file-type=elf
```

The mechanism is independent of which board generated those arguments.

---

# 14. Default Runner Selection

User configuration specifies:

```toml
[zephyr]
default = "local"
```

or:

```toml
[zephyr]
default = "remote"
```

During CMake configuration:

```text
local  -> openocd
remote -> remote-openocd
```

is written into generated flash/debug runner defaults.

Only two actual runner identities exist:

```text
openocd
remote-openocd
```

---

# 15. Automatic Default Regeneration

The module adds:

```text
~/.config/zephyr-remote-openocd/config.toml
```

to the CMake configure dependencies.

Expected flow:

```text
edit config.toml
      |
      v
west flash/debug
      |
      v
normal incremental build
      |
      v
CMake regeneration
      |
      v
updated runners.yaml
      |
      v
west reads new default
```

When rebuilding is explicitly suppressed, stale generated state may remain until reconfiguration.

This behavior was validated by PG-009 for the normal incremental west workflow.

---

# 16. Zephyr Runner Reuse Strategy

V1 may subclass and reuse the non-private interface of Zephyr 4.4's `OpenOcdBinaryRunner` where this materially reduces duplication.

The compatibility policy is:

> `runners.core` is Zephyr's explicitly supported external-runner API. `OpenOcdBinaryRunner` is reusable but is not covered by that compatibility guarantee. Any coupling to its non-private interface is Zephyr-version-specific, remains in the Zephyr compatibility layer, and excludes private attributes and methods.

The validated Zephyr 4.4 adapter reuses `capabilities()`, `do_add_parser()`, and the constructor. It overrides `name()`, `do_create()`, and `do_run()`. Constructor and version coupling is isolated in `zephyr44/runner.py`.

Supporting a new Zephyr release requires validation of this boundary or a version-specific adapter update.

---

# 17. Zephyr Compatibility Boundary

The Zephyr-specific layer owns:

- runner registration;
- parser integration;
- capabilities;
- reuse of OpenOCD runner options;
- flash semantics;
- GDB invocation semantics;
- RTT setup semantics;
- translation into generic remote-session requests.

It does not own:

- SSH implementation;
- staging transport;
- remote process supervision;
- loopback allocation;
- helper protocol.

---

# 18. Generic Remote Session Model

The generic subsystem receives structured data, conceptually:

```python
RemoteSessionRequest(
    remote_host=...,
    remote_openocd=...,
    ssh_command=...,
    openocd_args=...,
    environment=...,
    staged_files=...,
    services=...,
)
```

This subsystem has no dependency on a specific board or SoC.

---

# 19. Path Classification

Required local paths are handled through:

```text
explicit path mapping
        or
per-session staging
```

Algorithm:

```text
normalize path
     |
     +-- mapping matches?
          |
       +--+--+
       |     |
      yes    no
       |     |
   translate stage
```

Mappings are recursive and component-aware.

---

# 20. OpenOCD Search Trees

Large OpenOCD search trees which exist equivalently on both systems may be explicitly mapped.

Search paths supplied by Zephyr are preserved.

No assumption is made that a particular board uses or does not use files from a given search path.

Board-support directories from the active Zephyr checkout will typically be staged because they may contain local developer changes.

---

# 21. Flash Flow

```text
west flash
     |
     v
RemoteOpenOcdBinaryRunner
     |
     +-- resolve firmware and runner options
     +-- classify paths
     +-- create staging manifest
     +-- create remote session
     +-- stage files
     +-- construct remote OpenOCD command
     |
     v
remote helper
     |
     v
OpenOCD
     |
     v
target
```

There is no command-line re-parsing stage.

---

# 22. Debug Flow

```text
west debug
     |
     v
create remote session
     |
     v
stage configuration
     |
     v
start remote OpenOCD
     |
     v
wait for required service
     |
     v
establish forwarding
     |
     v
launch local GDB
     |
     v
debug session
     |
     v
cleanup
```

The runner controls client startup, eliminating the executable-facade startup race.

---

# 23. GDB Port Model

Structured runner state contains both the OpenOCD server and local client ports.

The mapping is deliberate:

```text
127.0.0.1:<gdb-client-port>
              |
              | SSH transport
              v
<remote-session-IP>:<gdb-server-port>
```

---

# 24. Attach and Debugserver

Attach uses the debug server setup without performing a load solely because the target is remote.

Debugserver creates and forwards the remote OpenOCD GDB service but does not automatically launch GDB.

Both remain board-agnostic.

---

# 25. RTT

The custom runner knows the RTT port before configuring RTT.

Flow:

```text
start remote OpenOCD
       |
establish enabled GDB/Tcl/telnet transports
       |
run local batch GDB (standalone west rtt)
       |
       +-- RTT setup
       +-- RTT start
       +-- RTT server start <port>
       |
establish RTT transport
       |
launch local RTT client
```

The RTT forward's local listener check is not treated as proof that its remote
channel opened. The foreground RTT client's connection is authoritative for
end-to-end reachability. The dedicated standard-library client provides
bidirectional channel-0 bytes, uses noncanonical/no-echo TTY input without
disabling normal signal handling, and restores the complete terminal state on
every exit path. Non-TTY input is supported without terminal operations.

For `debug --rtt-server` and `debugserver --rtt-server`, RTT setup is included
in OpenOCD's startup command sequence before its unique final readiness marker;
the RTT socket joins initial readiness and forwarding. These operations expose
the endpoint but do not launch a local RTT client. Standalone `rtt` reuses the
same remote OpenOCD version and Zephyr thread-info decision as debug/attach.
No GDB RSP observer is needed.

---

# 26. Semihosting

Semihosting console follows the normal OpenOCD process-output path:

```text
target
  |
  v
remote OpenOCD
  |
  | stdout/stderr
  v
remote helper
  |
  | SSH
  v
local west process
```

No semihosting-specific network subsystem exists.

---

# 27. Remote OpenOCD Service Isolation

Each OpenOCD session receives a random loopback address from:

```text
127.64.0.0/10
```

Different sessions therefore may use identical service-port numbers without collisions.

No board-specific addressing is involved.

---

# 28. Local Service Forwarding

The generic session layer uses explicit logical service descriptions:

```python
Service(
    name="gdb",
    local_port=...,
    remote_port=...,
)
```

Possible services include:

- GDB;
- Tcl;
- telnet;
- RTT.

Disabled services have no local listener.

---

# 29. SSH Command Abstraction

All SSH operations are built through one abstraction.

Conceptually:

```python
class SshCommand:
    argv_prefix: list[str]
```

Examples:

```python
["ssh"]
```

or, from WSL 2:

```python
["/mnt/c/Windows/System32/OpenSSH/ssh.exe"]
```

or:

```python
["ssh", "-F", "/home/user/.ssh/lab_config"]
```

Every SSH operation appends operation-specific arguments to this configured prefix.

The runner never assumes that the executable basename is literally `ssh`.

---

# 30. SSH Configuration Ownership

The selected SSH executable remains responsible for normal SSH behavior.

The remote-runner configuration identifies:

- the SSH client command;
- the remote host or alias.

It does not duplicate:

- private-key paths unless the user deliberately places them in fixed SSH command arguments;
- agent configuration;
- ProxyJump configuration;
- host-key configuration;
- host aliases.

This permits, for example, a WSL 2 user to invoke Windows `ssh.exe` and rely on the SSH environment already configured on Windows.

---

# 31. SSH Transport Capability Model

V1 correctness shall depend only on the subset of functionality required from the configured OpenSSH-compatible client.

Optional capabilities are treated separately.

Conceptually:

```text
required:
    execute remote command
    stdin/stdout streaming
    TCP forwarding

optional:
    ControlMaster / connection sharing
```

The transport implementation shall not make optional client capabilities prerequisites for correct operation.

---

# 32. ControlMaster and Multiplexing

ControlMaster is not part of the baseline transport contract.

If the selected SSH client supports connection multiplexing, the implementation MAY use it as an optimization.

If it does not, the session uses another topology.

Therefore:

```text
ControlMaster available
        |
       yes
        |
   optional optimization
```

versus:

```text
ControlMaster unavailable
        |
        v
normal supported transport
```

No user-visible feature is lost solely because multiplexing is unavailable.

---

# 33. Cross-Client SSH Topology

PG-014 validated multiple SSH processes as the V1 baseline topology without ControlMaster.

## 33.1 Selected baseline: multiple SSH processes

One SSH connection controls the helper.

Additional SSH processes provide forwarding.

Advantages:

- simple;
- relies on common OpenSSH behavior.

Disadvantages:

- may perform multiple authentications when no agent or multiplexing is available.

## 33.2 Retained alternative: one control session plus generic forwarding tunnel

A long-lived SSH connection may provide a generic forwarding facility while simultaneously running the helper.

Local Python proxies can then route service connections through that tunnel after the remote session address is known.

Advantages:

- can reduce authentication/session count;
- does not require ControlMaster.

Disadvantages:

- more local proxy logic.

The alternative remains available if WSL testing or operational experience shows that repeated authentication is unacceptable. PG-012 and PG-013 shall validate the selected baseline with WSL Linux OpenSSH and Windows OpenSSH invoked from WSL 2.

---

# 34. Staging Transport

Staging SHOULD use the configured SSH command rather than require a separate `scp` executable.

PG-015 validated arbitrary byte streaming through the configured native-Linux SSH command, including empty, textual, binary/NUL-containing, and 1 MiB payloads, with remote failure propagation. Production flash now uses this transport for validated session staging.

A preferred candidate is:

```text
local archive stream
       |
       | stdin of configured SSH command
       v
remote helper
       |
       v
private session staging directory
```

Advantages include:

- only one configurable SSH executable;
- consistent authentication behavior;
- no separate `scp` configuration;
- validated native-Linux `ssh` operation and use of the same configurable abstraction intended for WSL Linux `ssh` and Windows `ssh.exe`.

The WSL client variants remain subject to PG-012 and PG-013.

The staging manifest, safe archive encoding and extraction, private remote
filesystem layout, path rewriting, helper deployment protocol, and real OpenOCD
artifact staging are implemented by the protocol-1 helper and flash slice.

---

# 35. Remote Helper Deployment

The helper is automatically deployed to a per-user location such as:

```text
~/.local/libexec/zephyr-remote-openocd/
```

Deployment also uses the configured SSH command.

No assumption is made that the local SSH executable comes from the local Linux distribution.

---

# 36. Remote Helper Protocol

Protocol 1 is frozen. It uses UTF-8 JSON lines: one JSON object and one `LF` per
frame, with integer, non-Boolean `version: 1` and non-empty string `type`.
Helper stdout contains protocol frames only. Unknown fields are ignored for
historical tolerance, but senders MUST NOT use them as an extension mechanism.

The controller first emits `HELLO {helper}` and then
`SESSION_CREATED {session_id, remote_workspace}`; each field is a non-empty
string. Commands travel on controller stdin and events on controller stdout.
There is no feature negotiation beyond the mandatory version.

| Controller command | Required fields | Optional fields and behavior |
| --- | --- | --- |
| `START` | non-empty `services` list; each item has integer, non-Boolean `remote_port` in 1..65535 | Starts the test-only fake service once. Other service-object fields are returned unchanged. |
| `START_OPENOCD` | non-empty `argv` list of non-empty strings | `environment` defaults to `{}` and has string keys/values. `required_paths` defaults to `[]` and has `{kind: "file"\|"directory", path: string}`. `services` defaults to `[]` and has `{name: string, remote_port: 1..65535}`. `readiness_marker` is null/absent or a non-empty whitespace-free string. `readiness_timeout` is a positive non-Boolean number, default `30.0`. Starts once. |
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
SHA-256, and ordered staged relative paths; unsafe content fails the invocation.
`helper openocd-version <absolute-executable>` executes exactly
`<absolute-executable> --version`; success emits `OPENOCD_VERSION {output}`
with combined output as a string, while failure exits nonzero with `ERROR`.
Deployment bootstrap emits `DEPLOYED {status, path, sha256}`, where status is
`deployed`/`reused`, path is non-empty, and the digest has the same form.

A compatible protocol-1 pair implements this complete frozen contract, not
just the numeric version. Earlier pre-freeze helper contents at the
protocol-1 path are not a compatibility promise; digest deployment atomically
replaces them with matching source. Any incompatible change, or new client
behavior requiring helper support not guaranteed here, SHALL use protocol 2.
Bulk binary content remains stream-oriented instead of JSON/base64.

---

# 37. Remote Session Storage

Preferred:

```text
$XDG_RUNTIME_DIR/zephyr-remote-openocd/<session-id>/
```

Fallback:

```text
~/.cache/zephyr-remote-openocd/sessions/<session-id>/
```

Session data is private to the remote Unix user.

Persistent fallback data older than 24 hours may be cleaned opportunistically.

---

# 38. Process Supervision

OpenOCD executes in a helper-supervised process group.

Normal termination:

```text
local runner finishes
        |
        v
helper terminates OpenOCD
        |
        v
cleanup
```

Unexpected controlling-session loss follows the same cleanup path.

---

# 39. Local Runner State Machine

```text
NEW
 |
 v
CONFIGURE
 |
 v
CREATE_REMOTE_SESSION
 |
 v
STAGE
 |
 v
START_REMOTE_OPENOCD
 |
 v
WAIT_READY
 |
 v
START_FORWARDING
 |
 v
START_LOCAL_CLIENT
 |
 v
RUNNING
 |
 v
STOP_REMOTE
 |
 v
CLEANUP
 |
 v
DONE
```

Flash omits local-client stages when unnecessary.

---

# 40. Service Readiness

A dependent local client is not started until the required remote service is ready.

For persistent OpenOCD operations, the adapter appends a unique final `echo`
marker after the intended startup commands. The helper reports readiness only
after observing that marker and confirming that every requested service socket
is connectable. Disabled services impose no readiness check. The startup timeout
is 30 seconds.

This combines command-completion evidence with socket availability instead of
depending on ordinary human-readable OpenOCD diagnostics. The readiness design
is implemented and validated; it is no longer an open prototype decision.

---

# 41. OpenOCD stdout/stderr

Remote OpenOCD output is relayed with low buffering.

This includes:

- diagnostics;
- flash progress;
- GDB diagnostics;
- RTT diagnostics;
- semihosting console output.

Application console output is not interpreted or rewritten.

---

# 42. Error Handling

## Configuration

Identify the invalid/missing key and configuration path.

## SSH command

If the configured SSH executable cannot be started, identify the configured command.

## SSH authentication/connection

Report failure as an SSH transport error.

## Staging

Do not start OpenOCD with incomplete staging state.

## OpenOCD

Relay diagnostics and propagate failure.

## Local port conflict

Identify the service and port before launching its local client.

## Probe contention

Expose OpenOCD's normal acquisition failure.

## SSH loss

Fail the local operation and clean the remote OpenOCD session.

---

# 43. Suggested Code Boundaries

```text
python/zephyr_remote_openocd/
    config.py

    zephyr44/
        runner.py

    remote/
        model.py
        paths.py
        staging.py
        ssh.py
        services.py
        session.py
        helper_client.py

    helper/
        protocol.py
        deploy.py
```

Platform-specific SSH behavior, if any is eventually needed, shall remain inside the SSH transport layer rather than spread through runner logic.

---

# 44. Test Architecture

Successful prototype findings are protected by permanent regression suites organized as:

```text
tests/unit
tests/zephyr_integration
tests/ssh_integration
```

The exact directory layout is non-normative.

## 44.1 Unit tests

Unit tests cover configuration parsing and SSH command construction without Zephyr, SSH connectivity, OpenOCD, or hardware.

## 44.2 Hardware-free Zephyr integration

Runner registration and argument inheritance tests use generated Zephyr runner state without encoding specific board names in implementation logic.

The recording runner is permanent test infrastructure for the path:

```text
west
  -> runners.yaml
  -> runner argument parsing
  -> RunnerConfig
  -> remote-openocd adapter
  -> structured recording
```

Recording requires:

```text
ZEPHYR_REMOTE_OPENOCD_RECORD=1
```

These tests use recording mode or a harmless OpenOCD stub and do not access
physical hardware or execute real OpenOCD behavior. Without the explicit
recording setting, flash uses the validated production backend.

## 44.3 SSH integration

SSH integration tests cover native-Linux command selection, fixed arguments, streaming, remote failure propagation, and separate controller/forwarding processes without ControlMaster.

PG-012 and PG-013 remain present as permanent WSL-specific tests. They skip explicitly outside WSL 2 rather than being treated as passed.

## 44.4 Hardware integration

Hardware test fixtures may use concrete boards, but product code and general documentation remain board-agnostic.

Real OpenOCD flash validation covers two target configurations with materially
different board/SoC characteristics. Concrete target, probe, serial-device, and
expected-output values remain external fixture data.

At least one RTT-capable validation target SHALL exercise RTT; incapable
fixtures skip only RTT-specific assertions.

At least one capability-enabled validation target SHALL exercise semihosting
console output; unsupported fixtures skip only this capability assertion.

## 44.5 Acceptance audit

SRS section 31.1 maps every `AC-*` criterion to its maintained automated and/or
real-hardware evidence. Concurrent session isolation and controlling-SSH-loss
cleanup are covered by native-SSH fake-helper integration tests; they do not
need target-specific product behavior. PG-012 and PG-013 are the only deferred
acceptance validations, because they require WSL 2. Startup-overhead evidence
for REQ-NFUNC-PERF-001 is recorded in section 44.7.

## 44.7 Startup-overhead release evidence

REQ-NFUNC-PERF-001 states:

> Under representative local conditions, each invocation of `remote-openocd`
> SHOULD introduce less than 0.5 seconds of additional runner-controlled
> startup processing compared with the equivalent invocation of Zephyr's
> built-in `openocd` runner.

SSH authentication, external network latency, network-transfer time, and remote
OpenOCD initialization are excluded. The standalone
`tests/startup_benchmark.py` procedure measures west dispatch and runner setup
for built-in `openocd` (`--context`) and recording-mode `remote-openocd`.
`--context` is explicitly a conservative lower-bound proxy for the
corresponding built-in command path, not an exact execution comparison; this
makes the derived remote-minus-baseline overhead conservative. Recording mode
retains production adapter parsing, command construction, path planning,
environment selection, and request bookkeeping while eliminating the excluded
remote operations. Hardware target initialization is not measured.

The benchmark performs five warm-up iterations and 100 measured iterations per
case using `time.perf_counter_ns()`. Percentiles use the inclusive interpolated
rank `p * (n - 1)`. It reports first observation, median, p95, and worst value;
the target statistic is the paired remote-minus-baseline median, compared with
the 0.5-second SHOULD target. p95 and worst are diagnostic only. RTT is not
reported for builds without an RTT control block; no baseline is invented for
an inapplicable operation.

Release validation on 2026-09-04 used Python 3.14.7, native Linux
7.2.2-arch1-1, revision `1cf39b4`, and a Zephyr 4.4 build. Results (seconds of
additional startup) were:

| Command | First | Median | p95 | Worst | Target |
| --- | ---: | ---: | ---: | ---: | --- |
| flash | 0.02798 | 0.03664 | 0.08523 | 0.11606 | Meets target |
| debug | 0.02997 | 0.03854 | 0.09094 | 0.10939 | Meets target |
| attach | 0.05412 | 0.03864 | 0.09385 | 0.13740 | Meets target |
| debugserver | 0.03061 | 0.03795 | 0.08779 | 0.22082 | Meets target |

The benchmark is a manual release-validation artifact, not an ordinary CI
test or timing gate. The measured JSON record for this validation is
`doc/startup-overhead-2026-09-04.json`; future release records should preserve
the same schema and metadata.

## 44.6 Platform matrix

The completed native-Linux regression suite exercises:

- module discovery;
- recording flash and debug integration;
- SSH command execution and fixed arguments;
- forwarding without ControlMaster;
- binary streaming, failure propagation, and cleanup.

The remaining WSL validation shall exercise the corresponding SSH behavior with:

```text
WSL 2 + WSL Linux OpenSSH

WSL 2 + Windows OpenSSH ssh.exe
```

The Windows-client case validates that the SSH command abstraction does not depend on the WSL distribution's credentials or agent. Full real OpenOCD workflows on WSL 2 remain later hardware-integration work; native-Linux flash is validated.

---

# 45. Architecture Decisions Considered Final

Selected for V1:

- board-agnostic custom runner;
- no board/vendor-specific product behavior;
- native Linux and WSL 2 equally supported;
- runner name `remote-openocd`;
- built-in `openocd` retained;
- per-user local/remote default;
- two runner identities only;
- `EXTRA_ZEPHYR_MODULES`;
- self-contained Zephyr module;
- no pip/PyPI requirement;
- split Python implementation;
- Python setup script;
- commented configuration template;
- local default initially;
- CMake configure dependency;
- Zephyr-version-specific reuse of the non-private `OpenOcdBinaryRunner` interface only;
- configurable OpenSSH-compatible client command;
- default SSH command `ssh`;
- WSL 2 may use Windows `ssh.exe`;
- SSH command may contain fixed arguments;
- all SSH operations use the configured client abstraction;
- V1 does not depend on ControlMaster;
- connection multiplexing is optional;
- system/client SSH configuration remains authoritative;
- unprivileged remote helper;
- explicit path mappings with staging fallback;
- local GDB;
- remote OpenOCD;
- per-session remote loopback isolation;
- structured RTT handling;
- semihosting console via OpenOCD stdout/stderr;
- no persistent artifact cache;
- fail-fast cleanup after SSH loss.

---

# 46. Validated Prototype Results

The permanent native-Linux suite retains regression coverage for every completed
prototype gate. The two WSL-specific tests for PG-012 and PG-013 skip explicitly
outside WSL 2.

The following gate identifiers are retained for traceability and SHALL NOT be reused:

- **PG-001 — PASS:** discovered the self-contained module through `EXTRA_ZEPHYR_MODULES` alone.
- **PG-002 — PASS:** used the module with an in-tree Zephyr sample without repository changes.
- **PG-003 — PASS:** used the same module with an out-of-tree application without repository changes.
- **PG-004 — PASS:** registered `remote-openocd` only when `openocd` was present.
- **PG-005 — PASS:** mirrored applicable OpenOCD runner arguments exactly.
- **PG-006 — PASS:** received common `RunnerConfig` normally.
- **PG-007 — PASS:** retained and explicitly selected `-r openocd`.
- **PG-008 — PASS:** instantiated the recording runner with `-r remote-openocd` under the explicit test-only recording setting.
- **PG-009 — PASS:** a configuration-only default change triggered CMake regeneration before runner selection during normal `west flash` and `west debug` operation.
- **PG-010 — PASS:** characterized the reuse boundary. The Zephyr 4.4 adapter reuses `capabilities()`, `do_add_parser()`, and the constructor; overrides `name()`, `do_create()`, and `do_run()`; accesses no private `OpenOcdBinaryRunner` implementation; and confines constructor/version coupling to `zephyr44/runner.py`.
- **PG-011 — PASS:** ran a basic remote command against the configured SSH fixture with native Linux `ssh`, including a configured command with fixed arguments.
- **PG-014 — PASS:** validated separate controlling and `ssh -L` processes with `ControlMaster=no`, including removal of the remote endpoint when the controlling session closed.
- **PG-015 — PASS:** validated empty, textual, binary/NUL-containing, and 1 MiB streams through the configured SSH command, verified byte count and SHA-256, and propagated remote failure.

---

# 47. Remaining Prototype Gates

## PG-012 — DEFERRED

Run the same helper command from WSL 2 using WSL's Linux `ssh`.

This gate requires a WSL 2 test environment.

## PG-013 — DEFERRED

Run the same helper command from WSL 2 using configured Windows `ssh.exe`.

This gate requires a WSL 2 test environment and remains especially relevant to RISK-007.

---

# 48. Remaining Implementation Decisions

The remaining non-blocking implementation considerations are optional SSH
multiplexing and internal module layout beyond the validated compatibility
boundary. Neither changes the frozen V1 interfaces or requires a product
decision for the validated V1 capability.

Persistent-debug readiness, enabled-service forwarding, remote OpenOCD version
probing, and Zephyr thread-info command selection are settled implementation
decisions described in Sections 40 and 52, not remaining questions.
The Python setup entry point and V1 configuration schema are also complete and
frozen as described in Sections 7 and 8.

---

# 49. Phase Transition

```text
Zephyr integration prototype
    COMPLETE except deferred WSL-specific PG-012 and PG-013

Permanent regression transformation
    COMPLETE

User setup UX and V1 configuration schema
    COMPLETE

Remote-session fake-helper vertical slice
    COMPLETE (native-Linux integration remains fixture-gated)

Real OpenOCD flash
    COMPLETE and validated on native Linux

Debug, attach, and debugserver
    COMPLETE and validated on native Linux

RTT
    COMPLETE and validated on RTT-capable hardware

Semihosting console
    COMPLETE on every configured capable fixture
```

---

# 50. Protocol-1 Fake-Helper Vertical Slice

The first production transport slice uses a single-file, Python-standard-library
helper and protocol version 1. Control messages and events are newline-delimited
JSON objects carrying mandatory `version` and `type` fields. Helper stdout is
reserved for those objects. Child stdout and stderr are captured and represented
as typed `CHILD_OUTPUT` events.

The helper is streamed through the configured SSH command and atomically installed
at `~/.local/libexec/zephyr-remote-openocd/protocol-1/helper.py`. A SHA-256 match
reuses the existing mode-0600 file; replacement does not accumulate versioned
copies. No administrative privilege is required.

Each controller owns an unpredictable, mode-0700 workspace below
`$XDG_RUNTIME_DIR/zephyr-remote-openocd`, or below
`~/.cache/zephyr-remote-openocd/sessions` when no runtime directory is available.
It owns cleanup after normal stop, malformed protocol, signal, child failure, or
control-channel EOF.

Staging uses a POSIX tar archive held in `SpooledTemporaryFile`, spilling beyond
its memory bound. It is therefore bounded-buffer staging, not end-to-end
streaming. Extraction does not use `extractall()` or Python's version-dependent
default filters: every member must be a unique, normalized relative regular file;
links, special files, traversal, and escaping destinations are rejected before
content is written.

For the fake-service workload only, the helper attempts up to 32 random IPv4
addresses in `127.64.0.0/10` and reports one only after its listeners are bound.
The client creates separate
`ssh -L` processes with `ControlMaster=no`, `ExitOnForwardFailure=yes`, exact
ports, and a `127.0.0.1` local bind. Port preflight is advisory; the forwarding
process is authoritative. Session health is process-driven through `poll()` and
`wait()`, and cleanup unwinds forwards in reverse creation order before stopping
the controller.

---

# 51. Real OpenOCD Flash Slice

Status: **COMPLETE — validated V1 capability on native Linux.**

Production `flash` translates the public Zephyr 4.4 runner state into explicit
remote argv. Search trees, configuration files, and firmware use the longest
matching component-aware path mapping, with private-session staging as fallback.
Staged directory symlinks are dereferenced only within their source tree. The
helper verifies mapped paths before launch.

The helper expands only its workspace and allocated-loopback placeholders and
executes OpenOCD without a shell in a supervised process group. It injects the
allocated address through OpenOCD's `bindto` command but does not change GDB,
Tcl, or telnet port behavior supplied by Zephyr and OpenOCD configuration.
Output is continuously relayed as protocol events and the OpenOCD exit status
becomes the west flash result.

`ZEPHYR_REMOTE_OPENOCD_RECORD=1` remains permanent no-I/O integration
infrastructure and takes precedence over production execution. Remote serial
observation belongs only to explicitly configured hardware acceptance tests and
is not part of production runner semantics.

## 51.1 Hardware-test evidence

Two target configurations validated the production path end to end. Both used
real Zephyr-tree OpenOCD configuration staging, a mapped OpenOCD scripts tree,
firmware-path rewriting, a helper-allocated loopback address, local output relay,
remote cleanup, and fresh post-flash serial output as a test-only oracle.

- One fixture validated explicit `--serial` probe selection while exercising an
  allow-listed environment variable through the runner/helper process request.
  The hardware fixture did not provide an independent assertion that OpenOCD's
  configuration consumed that variable; allow-list selection, omission, and
  helper-before-child ordering are covered by permanent recording/unit tests.
- A second fixture validated normal OpenOCD automatic probe selection without
  `--serial` when a single applicable probe was present.

Concrete target, probe, serial-device, baud-rate, and expected-output values
remain in external test-fixture configuration. Production configuration,
translation, staging, helper supervision, and serial-oracle infrastructure
contain no target-specific behavior.

---

# 52. Persistent Debug, Attach, and Debugserver Slice

Status: **COMPLETE — validated V1 capability on native Linux.**

One helper-supervised OpenOCD process persists for each operation. A unique
final OpenOCD `echo` marker proves that command-specific initialization has
completed; readiness additionally checks enabled service sockets without
consuming OpenOCD's single-client GDB slot (the real GDB handshake is
authoritative for GDB). Exact loopback-only SSH forwards expose remote GDB, Tcl, and telnet
services. Disabled services create neither a readiness requirement nor a local
listener. The remote GDB server port maps to Zephyr's distinct local GDB-client
port where configured.

Debug and attach preserve Zephyr 4.4 halt/no-halt behavior. Debug launches local
GDB and loads by default, attach launches it without loading, and GDB init
commands retain their position after the optional load. Debugserver preserves
reset/halt startup, exposes the local endpoint, and launches no local GDB.
Remote debugserver intentionally honors `--serial`, a documented divergence
from the Zephyr 4.4 built-in runner.

When the build requests thread information, the backend executes the configured
OpenOCD path remotely with `--version`. The shared parser enables Zephyr RTOS
awareness only under Zephyr 4.4's version condition. Recording mode performs no
I/O and supplies a pre-resolved version through its explicit test seam. Hardware
validation is capability-based: both fixture configurations exercise all three
operations, while one capable configuration additionally enumerates a real
Zephyr thread.

Protocol-1 service/readiness fields and remote version probing are frozen in
the wire contract in section 36. Digest deployment replaces an older helper
with matching source; future client-required helper behavior uses a new
protocol version rather than extending protocol 1.

---

# 53. RTT Slice

Status: **COMPLETE — validated on RTT-capable native-Linux hardware.**

Standalone `west rtt` starts persistent remote OpenOCD with the enabled normal
services, uses local batch GDB for the Zephyr 4.4 RTT command sequence, then adds
the structured RTT forward and launches the lifecycle-aware local byte client.
`debug --rtt-server` and `debugserver --rtt-server` instead include RTT setup in
OpenOCD startup readiness and expose the endpoint without launching that client.

Hardware acceptance used an externally selected capable fixture and validated a
non-default RTT port, real Zephyr channel-0 output and input, simultaneous GDB
and RTT forwarding, debugserver endpoint-only behavior, interruption handling,
and complete remote workspace/process cleanup. Concrete hardware identity and
RTT capability remain solely in ignored fixture configuration. Semihosting is
not part of this slice.

# 54. Direct Semihosting Console Validation

Status: **COMPLETE — validated through the existing OpenOCD stdout/stderr
relay on every configured capable native-Linux hardware fixture.** Both normal
completion and interruption-triggered termination passed, including workspace,
controller, OpenOCD, and forwarding cleanup.

Fixture-supplied OpenOCD commands are passed through `--cmd-pre-init`; the
validated fixtures register the direct-mode commands with OpenOCD's
`post_init_commands` so the normal runner sequence remains configuration,
`init`, semihosting setup, and then GDB load. An explicit fixture-only `init`
is permitted only as a fallback when a particular OpenOCD build cannot use
`post_init_commands`. The direct-mode sequence enables semihosting and disables
GDB File-I/O and TCP redirection. Fixture-supplied GDB commands are limited to
control actions such as resuming and orderly termination; they do not configure
semihosting.

The test observes fresh target console text in relayed OpenOCD stdout/stderr
and exercises both orderly and interruption cleanup. Semihosting exit or halt is
preferred but not required when the fixture can perform separate orderly
termination after the output oracle matches. Hardware capability is declared
externally, so unsupported combinations skip only this assertion. No
semihosting proxy, filesystem virtualization, GDB File-I/O handling, or
board-specific production logic was added.

The earlier no-output observation came from the test lifecycle ordering
(`flash` followed by a no-load debug session), which could let a one-shot
application run before semihosting was enabled. The permanent flow performs a
normal GDB load after post-init semihosting configuration. The earlier local GDB
reset was independently traced to stale local SSH forwarding processes; the
GDB handshake is now authoritative and no readiness probe consumes OpenOCD's
single-client GDB socket.
