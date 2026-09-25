# Zephyr Remote OpenOCD Software Architecture Document

## Navigation

- [Purpose, drivers, and overview](#1-purpose)
- [Module structure and setup](#5-self-contained-zephyr-module)
- [Zephyr runner integration](#9-module-discovery)
- [Remote execution planning](#18-generic-remote-session-model)
- [Services, SSH, staging, and lifecycle](#27-remote-openocd-service-isolation)
- [Errors, code boundaries, tests, and decisions](#42-error-handling)

## 1. Purpose

This document describes the current architecture for the Zephyr west runner for remote OpenOCD.

The SRS defines externally required behavior.

This document describes implementation structure and architectural decisions.

---

## 2. Architecture Drivers

The primary drivers are:

- no development-repository changes;
- board-agnostic design;
- one Linux implementation;
- Zephyr 4.4 compatibility;
- local GDB and remote OpenOCD;
- reuse of existing board OpenOCD configuration;
- concurrent remote sessions;
- user-selectable SSH client;
- minimal remote administration;
- maintainable Zephyr-version isolation.

---

## 3. Architecture Overview

```text
                   LOCAL HOST
                  Linux

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
       configured SSH client
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

## 4. Supported Local Platform

The local platform is Linux with Python 3.12 or newer. The generic Python
implementation uses one Linux architecture and does not select platform
backends.

---

## 5. Self-Contained Zephyr Module

The project is distributed as a self-contained Zephyr module rather than an installed Python distribution.

Current implementation structure:

```text
zephyr_remote_openocd/
    zephyr/
        module.yml
        CMakeLists.txt
        config_default.py

    runners/
        remote_openocd.py

    python/
        zephyr_remote_openocd/
            __init__.py
            config.py

            resources/
                configuration.schema.json

            zephyr44/
                runner.py

            remote/
                model.py, paths.py, staging.py, ssh.py
                services.py, session.py, forwarding.py
                helper_client.py, cleanup.py, protocol.py
                backend.py, deploy.py, debug.py, flash.py, rtt.py
                openocd_plan.py

            remote_helper.py

    resources/
        config.example.yaml

    scripts/
        user/
            setup.py
            validate_configuration.py
        contributor/
            static_check.py
            validate_hardware_inventory.py
```

The implementation is intentionally self-contained in the module tree. Apart
from the discovery markers described below, exact filenames are not
architectural contracts. User setup is implemented by `scripts/user/setup.py`; pip
packaging is not required. `zephyr/config_default.py` is invoked by the adjacent
`CMakeLists.txt` during build configuration; it is not a user command.

---

## 6. Python Import Model

Zephyr loads the external runner entry point from the module.

The entry point only bootstraps the module's own Python tree.

The entry point searches its ancestors for the module manifest and importable
Python package. It does not assume a fixed number of parent directories.
Conceptually:

```python
from pathlib import Path
import sys

module_root = find_ancestor_containing(
    "zephyr/module.yml",
    "python/zephyr_remote_openocd/__init__.py",
)
sys.path.insert(0, str(module_root / "python"))

from zephyr_remote_openocd.zephyr44.runner import (
    RemoteOpenOcdBinaryRunner,
)
```

The substantive implementation remains split into normal Python modules.

The local runner may use `pyelftools` for ELF inspection and PyYAML plus
jsonschema for configuration loading. Zephyr 4.4's configured Python environment
already provides these accepted runtime dependencies; they are not functionality
to reimplement. Setup diagnoses missing imports and directs users back to that
environment. The module does not require pip packaging or a separate dependency
installation path.

---

## 7. Distribution and User Setup

Distribution places the complete module at an arbitrary persistent path. A
convenient documented example is:

```text
~/zephyrproject/zephyr_remote_openocd
```

This path is only an example; the module may live anywhere persistent.

User setup is a separate, non-invasive operation:

```text
python3 scripts/user/setup.py
```

The setup script copies `resources/config.example.yaml` only when the canonical
per-user configuration is absent, reports the created/reused status and both
absolute paths, and prints guidance for `EXTRA_ZEPHYR_MODULES`, editing the
configuration, and running the configuration validator. It creates the
`zephyr_remote_openocd` configuration directory with mode `0700` and the file
with mode `0600`; existing parents, directories, and files are never chmodded.
It does not edit shell startup files, repositories, or `.zephyrrc`.

---

## 8. Configuration Template

The canonical template is `resources/config.example.yaml`:

```yaml
default_runner: openocd
remotes:
  lab:
    ssh_host: replace-with-ssh-host-or-alias
    openocd_command:
      - /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd
    ssh_command: [ssh]
    forward_env: []
    path_mappings: {}
```

The shipped comments show how to select this remote by default, define and use
a preset, forward environment names, and map an existing remote resource. The
placeholder remote is not selected by default, and `default_runner: openocd`
keeps local OpenOCD active until the user deliberately opts into remote
operation.

`python/zephyr_remote_openocd/resources/configuration.schema.json` defines the
configuration's structure and lexical rules. The loader reads it by package
identity through `importlib.resources`, independently of repository depth. The
loader safely parses YAML, rejects duplicate keys and explicit nulls, then
validates the document against that schema. The schema alone validates command
and path spelling, including NUL exclusion and normalized remote path syntax.
Code then expands and resolves local mapping keys and detects collisions because
those operations depend on the local filesystem. Remote references and required
`openocd_command` are checked only when the selected remote is used, allowing
incomplete unused definitions.

Remote fields replace complete preset settings; lists and mappings are not
merged. Remote `~` paths are expanded using the SSH user's actual home only
during a real operation; recording keeps them unresolved.

`scripts/user/validate_configuration.py` is a no-I/O front end to this loader and
resolver. A non-empty `ZEPHYR_REMOTE_OPENOCD_CONFIG` overrides the product
default configuration path, and a leading current-user `~` is expanded before
the file is read. The validator resolves an explicit `--remote`, otherwise the
file's `default_remote`; it does not consult
`ZEPHYR_REMOTE_OPENOCD_REMOTE`, so shell state cannot silently change the
summary. With no selected remote it reports structural validity and available
definitions. It requires the target file to exist, prints commands as argv and
forwarded environment names without values, and does not test local or remote
resource existence.

The SSH command is represented as an argv list rather than a shell command string.
An argv representation:

- avoids shell quoting ambiguity;
- permits fixed arguments;
- avoids unnecessary shell invocation;
- works naturally with Python `subprocess`;
- permits a bare executable name or explicit executable path.

This remains analogous in purpose to `GIT_SSH_COMMAND` without requiring shell-string semantics.

Runtime resources are owned and located by the Python package. Zephyr entry
points find the self-contained module by its manifest and package markers.
Git-dependent development scripts ask Git for the repository root, while tests
share marker-based root discovery from `tests/support.py`. The markers and
importable package are structural contracts; their nesting depth is not.

---

## 9. Module Discovery

The module is supplied through:

```text
EXTRA_ZEPHYR_MODULES=<module-root>
```

The project does not prescribe how the user stores this setting.

Documentation shall show at least one convenient repository-independent approach.

---

## 10. External Runner Registration

`zephyr/module.yml` declares:

```yaml
name: zephyr_remote_openocd

runners:
  - file: runners/remote_openocd.py
```

The custom Python runner reports:

```text
remote_openocd
```

Python discovery is complemented by CMake integration because west also validates runner availability from generated build runner state.

---

## 11. Conditional Build-Time Runner Augmentation

For a build with:

```text
openocd
```

the module adds:

```text
remote_openocd
```

and mirrors applicable built-in OpenOCD runner arguments.

For a build without `openocd`, the module does not add `remote_openocd`.

No board name, vendor name, architecture, or SoC family participates in this decision.

Eligibility is based exclusively on existing OpenOCD runner support.

---

## 12. Common Runner Configuration

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

## 13. Runner-Specific Argument Mirroring

The CMake compatibility layer mirrors applicable arguments associated with:

```text
openocd
```

to:

```text
remote_openocd
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

  remote_openocd:
    - --cmd-load
    - flash write_image erase
    - --cmd-verify
    - verify_image
    - --file-type=elf
```

The mechanism is independent of which board generated those arguments.

---

## 14. Default Runner Selection

User configuration specifies:

```yaml
default_runner: openocd
```

or:

```yaml
default_runner: remote_openocd
```

During CMake configuration:

```text
openocd        -> openocd
remote_openocd -> remote_openocd
```

is written into generated flash/debug runner defaults.

Only two actual runner identities exist:

```text
openocd
remote_openocd
```

---

## 15. Automatic Default Regeneration

The module adds the effective configuration path as a CMake configure
dependency. By default, that path is:

```text
~/.config/zephyr_remote_openocd/config.yaml
```

A non-empty `ZEPHYR_REMOTE_OPENOCD_CONFIG` selects the effective path for both
configuration loading and dependency registration.

Expected flow:

```text
edit config.yaml
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

---

## 16. Zephyr Runner Reuse Strategy

The runner may subclass and reuse the non-private interface of Zephyr 4.4's
`OpenOcdBinaryRunner` when doing so reduces duplication.

The compatibility policy is:

> Zephyr supports `runners.core` as its external-runner API. It does not make
> that compatibility guarantee for `OpenOcdBinaryRunner`. The adapter may use
> the class's non-private interface, but that code remains version-specific and
> confined to the Zephyr compatibility layer.

The Zephyr 4.4 adapter reuses `capabilities()` and the constructor. It overrides
`name()`, `do_create()`, `do_add_parser()`, and `do_run()`. The parser override
delegates to `OpenOcdBinaryRunner.do_add_parser()` before adding `--remote`.
Constructor and version coupling is isolated in `zephyr44/runner.py`.

Supporting a new Zephyr release requires validating this interface or updating
the version-specific adapter.

---

## 17. Zephyr Compatibility Boundary

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

## 18. Generic Remote Session Model

The generic subsystem receives structured data, conceptually:

```python
RemoteSessionRequest(
    host=...,
    ssh_command=...,
    process=RemoteProcess(...),
    staged_files=...,
    services=...,
)
```

This subsystem has no dependency on a specific board or SoC.

---

## 19. Path Classification

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

## 20. OpenOCD Search Trees

Large OpenOCD search trees which exist equivalently on both systems may be explicitly mapped.

Search paths supplied by Zephyr are preserved.

No assumption is made that a particular board uses or does not use files from a given search path.

Board-support directories from the active Zephyr checkout will typically be staged because they may contain local developer changes.

---

## 21. Flash Flow

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

Flash command construction is phase-oriented: a shared immutable OpenOCD
prefix is combined with a resolved image plan and one concrete ELF, BIN, or
HEX operation plan. The public flash-plan result remains the runner boundary.

---

## 22. Debug Flow

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
wait for lifecycle readiness
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

Persistent debug construction similarly separates immutable service/RTT
validation, OpenOCD server commands, and local GDB arguments before assembling
the public debug-plan result.

---

## 23. GDB Port Model

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

## 24. Attach and Debugserver

Attach uses the debug server setup without performing a load solely because the target is remote.

Debugserver creates and forwards the remote OpenOCD GDB service but does not automatically launch GDB.

Both remain board-agnostic.

---

## 25. RTT

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

The RTT forward's local listener does not prove that its remote channel opened.
The foreground RTT client must connect to establish end-to-end reachability.
The dedicated standard-library client provides
bidirectional channel-0 bytes, uses noncanonical/no-echo TTY input without
disabling normal signal handling, and restores the complete terminal state on
every exit path. Non-TTY input is supported without terminal operations.

For `debug --rtt-server` and `debugserver --rtt-server`, RTT setup is included
in OpenOCD's startup command sequence before its startup-complete sentinel.
OpenOCD owns the RTT listener; the helper does not probe it. These operations
expose the endpoint but do not launch a local RTT client. Standalone `rtt` reuses the
same remote OpenOCD version and Zephyr thread-info decision as debug/attach.
No GDB RSP observer is needed.

---

## 26. Semihosting

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

The runner explicitly supports semihosting console output through this normal
relay and accepts ordinary OpenOCD commands that enable it. It does not
configure, proxy, virtualize, or translate paths for GDB File-I/O. Remote
OpenOCD may transparently pass File-I/O requests to a locally connected GDB,
in which case GDB can access its local host filesystem without runner
involvement. Other operations handled directly by OpenOCD execute on the
remote host according to OpenOCD behavior. These transparent behaviors are
outside the runner's compatibility guarantees.

---

## 27. Remote OpenOCD Service Isolation

The helper allocates each OpenOCD session a random loopback address from:

```text
127.64.0.0/10
```

Different sessions therefore may use identical service-port numbers without collisions.

The helper temporarily binds the requested ports at candidate addresses to
preflight collisions, then releases those sockets before starting OpenOCD.
OpenOCD owns the actual enabled GDB, Tcl, telnet, and RTT listeners on the
allocated address; the helper neither creates nor probes those listeners.

No board-specific addressing is involved.

---

## 28. Local Service Forwarding

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

The forward manager owns the local SSH-forward processes and their local
loopback endpoints. The corresponding remote listeners remain owned by
OpenOCD.

---

## 29. SSH Command Abstraction

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

or with an alternate explicit path:

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

## 30. SSH Configuration Ownership

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

This permits users to select another OpenSSH-compatible executable and rely on
the configuration, credentials, and agent behavior provided by that client.

---

## 31. SSH Transport Capability Model

Correctness shall depend only on the subset of functionality required from the configured OpenSSH-compatible client.

Conceptually:

```text
required:
    execute remote command
    stdin/stdout streaming
    TCP forwarding
```

---

## 32. SSH Connection Sharing

Connection sharing is not part of the current design.

---

## 33. Cross-Client SSH Topology

The topology uses multiple SSH processes.

### 33.1 Current design: multiple SSH processes

One SSH connection controls the helper.

Additional SSH processes provide forwarding.

Advantages:

- simple;
- relies on common OpenSSH behavior.

Disadvantages:

- may perform multiple authentications when no agent or multiplexing is available.

## 34. Staging Transport

Staging SHALL use the configured SSH command rather than require a separate
`scp` executable.

Helper deployment and the long-lived helper-control and forwarding processes
also use the same configured command abstraction; no implicit `scp` or SFTP
transport is used.

The configured SSH command carries arbitrary byte streams, including empty,
textual, binary/NUL-containing, and large payloads, with remote failure
propagation. Production flash uses this transport for session staging.

Staging is a finite transfer: the locally built, seekable archive is supplied
as the configured SSH command's stdin while that command's output is captured.
It has no general live-producer streaming subsystem. Long-lived helper and
forwarding SSH processes instead own one bounded background stderr drain so
diagnostics cannot block their control or forwarding traffic.

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
- use of the same configurable abstraction for any selected client.

The Protocol v1 helper and flash implementation handle the staging manifest,
safe archive encoding and extraction, private remote filesystem layout, path
rewriting, helper deployment, and OpenOCD artifact staging. Staging manifests
carry explicit directory entries, including empty roots and nested
directories; file byte counts and digests cover regular-file content only.

---

## 35. Remote Helper Deployment

The helper is automatically deployed to a per-user location such as:

```text
~/.local/libexec/zephyr_remote_openocd/
```

Deployment also uses the configured SSH command.

No assumption is made that the local SSH executable comes from the local Linux distribution.

---

## 36. Remote Helper Protocol

The current internal helper wire format and behavior are specified in
[protocol.md](protocol.md). Helper stdout contains only JSON protocol frames.
The deployed client and helper implement one strict contract; the numeric wire
value remains `1` as its identifier and is not an external compatibility
guarantee.

## 37. Remote Session Storage

Preferred:

```text
$XDG_RUNTIME_DIR/zephyr_remote_openocd/<session-id>/
```

Fallback:

```text
~/.cache/zephyr_remote_openocd/sessions/<session-id>/
```

Session data is private to the remote Unix user.

Persistent fallback data older than 24 hours may be cleaned opportunistically.

---

## 38. Process Supervision

The lifecycle has distinct operation, session, subsystem, process, and remote
ownership boundaries:

- The operation or runner owns when the `RemoteSession` lifetime ends.
- `RemoteSession` is the sole whole-session coordinator and transitive owner
  of local session resources. It owns staging orchestration, cleanup ordering
  between subsystems, and cross-subsystem failure precedence.
- `_HelperClient` owns the helper control connection, its protocol reader,
  helper-local synchronized lifecycle state, output delivery, and helper
  shutdown transaction.
- `_ForwardManager` owns the forwarding SSH processes, readiness and health
  checks, and forwarding cleanup transaction.
- `ManagedSshProcess` owns one local SSH subprocess and its stderr drain.
- The remote `ControlSession` owns remote session state and workspace, while
  `SupervisedChild` owns the OpenOCD process group and its output relays.

`RemoteSession.close()` invokes subsystem cleanup in the required order and
decides which failure is primary across helper and forwarding cleanup.

### 38.1 Local SSH subprocess ownership

`ManagedSshProcess` remains a narrow ownership wrapper rather than a session
abstraction. It delegates process status and termination to the underlying
SSH subprocess and owns exactly one `_StderrDrain`. Standard input and output
remain available to the session protocol or forwarding-readiness owner, while
the stderr pipe is detached from the subprocess object and transferred to the
drain so that it has only one local owner.

The per-process drain thread is required because a long-lived SSH client may
emit more diagnostic data than an operating-system pipe can hold while its
stdout still carries protocol or readiness data. The drain retains only a
bounded byte tail. A lock protects that tail because failure observation may
read it while the drain thread is still appending data.

Shutdown waits for drain completion and thread exit within one shared bounded
budget before closing the stderr stream. Closing a buffered pipe while another
thread is blocked in `read()` can itself block on the stream's internal lock.
If the SSH process or a descendant still holds the write side and EOF does not
arrive, cleanup therefore reports failure and retains the stream rather than
turning stream disposal into an unbounded wait. Close serialization and an
explicit closed flag keep repeated cleanup attempts harmless. The drain thread
is a daemon so an uncooperative inherited writer cannot hold local process
shutdown open indefinitely.

This separation is intentional: `_HelperClient` and `_ForwardManager`
perform process cleanup within their own subsystem transactions,
`RemoteSession.close()` orders those transactions, `ManagedSshProcess`
exposes per-process control and diagnostic access, and `_StderrDrain` alone
owns stderr consumption and disposal. Removing the wrapper, drain thread,
bounded tail, or bounded reader shutdown would either introduce dual ownership,
permit pipe backpressure to stall the session, lose actionable SSH
diagnostics, or make cleanup potentially unbounded. The per-process wrapper
does not replace the helper and forwarding resource owners.

`RemoteSession` is the sole local whole-session coordinator and transitive
resource owner. It is acquired once through `RemoteSession.open()`, which
returns only a usable session, and is released once through cleanup-only
`RemoteSession.close()`. The helper client and forward manager own their
respective resources and cleanup transactions beneath this boundary. A session
is one-shot and cannot be reopened or restarted.

OpenOCD executes in a helper-supervised process group and session. The process
group is the helper's ownership boundary for generic cleanup hygiene, including
processes that outlive the OpenOCD leader.

The helper's `ControlSession` owns the workspace, control selector, command
dispatch, signal handlers, and final cleanup. A `SupervisedChild` owns the
configured OpenOCD process, output relays, readiness observation, termination,
and stream closure. The helper allocates the remote loopback address and
preflights requested service ports for bind collisions; OpenOCD owns and
configures the actual GDB, Tcl, telnet, and RTT listeners. The helper does not
probe listener connectability. This keeps process resources attached to one
owner across success, failure, EOF, and signal paths.

Cleanup sends `SIGTERM` to the owned group and waits a bounded grace period for
the leader. It then checks whether the group still exists. If so, the helper
may inspect `/proc` once and warn about observable non-leader members before
sending `SIGKILL`. Failure or a race during this best-effort diagnostic does
not affect the group cleanup decision. The helper then reaps the leader, joins
output relays, and releases their streams. It does not continuously monitor
the process tree, retain descendant PID history, or use descendant discovery
to decide whether the group needs cleanup.

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

For a client-requested stop, protocol completion accepts a valid session-close
`SESSION_CLOSED` event with either `reason: "requested"` and
`returncode: null`, or `reason: "process_exit"` and an integer return code.
The latter also records that value as `openocd_returncode`. Successful local
cleanup additionally requires the helper to exit with status zero. Protocol,
helper, or transport failures remain visible to the caller; later mechanical
cleanup failures are retained as diagnostics. A received `ERROR` remains the
helper failure across cleanup, rather than becoming a second reader or cleanup
failure. Local shutdown attempts all remaining mechanical cleanup once and
then marks the session closed. A later `close()` is harmless, but does not
resume a partially failed cleanup transaction or retain resources solely for
that purpose.

Unexpected controlling-session loss follows the same cleanup path.
Each session holds an advisory lock in its workspace. When allocating a new
session, the helper opportunistically removes session workspaces older than 24
hours when their lock is no longer held or lock creation never completed. This
reclaims state left by uncatchable termination without disturbing concurrent
active sessions.

---

## 39. Local Session Lifecycle

```text
prepare operation
       |
RemoteSession.open()
       |
       +-- deploy helper
       +-- create control session
       +-- stage files
       +-- start OpenOCD
       +-- wait for readiness
       +-- establish initial forwarding
       |
usable RemoteSession
       |
run local operation
       |
RemoteSession.close()
       |
done
```

The helper reader distinguishes three local outcomes:

- A received `SESSION_CLOSED` is an orderly session close. Its reason is
  recorded; only `reason: "process_exit"` supplies an OpenOCD result.
- A received `ERROR` is a valid protocol failure event that ends the session.
  The caller sees the helper error itself, not an event-stream failure. No
  later `SESSION_CLOSED` is required.
- An event-stream, read, or validation failure is distinct from both events.
  It includes malformed or out-of-order messages and transport loss without a
  session-ending event, and is reported as a reader or transport failure.

After an accepted `ERROR`, the background reader stops without recording a
reader failure. Mechanical cleanup still disposes of owned resources, but
does not send `STOP`, await `SESSION_CLOSED`, or report the same `ERROR` again as
a cleanup failure. If the foreground has not yet received the `ERROR`,
`close()` reports that helper failure once; otherwise it reports only
independent cleanup failures under the existing failure-precedence rules.

The public lifecycle does not require state enumeration. Flash and other
operations may omit local-client work while retaining the same session
acquisition and cleanup boundary.

---

## 40. Service Readiness

The runner starts dependent local clients only after the requested process
readiness policy is satisfied; this does not experimentally verify every
exposed service endpoint.

For persistent OpenOCD operations, the adapter places an init-complete echo
hook in OpenOCD's post-init command list before board configuration files. It
also establishes the remote bind address and exposed service-port settings
before those files. These are runner-owned session and transport properties,
so a configuration-triggered `init` cannot create listeners with pre-runner
defaults. Board configuration files own probe and target setup. A board or user
configuration that overrides the runner's bind address or service ports
conflicts with the remote-session contract. The hook runs after OpenOCD
initialization has created its GDB listener. The adapter then appends a
startup-complete echo after the full server startup sequence.
Each complete trimmed output sentinel proves one lifecycle fact; the helper emits
`PROCESS_READY` only after both have been observed, in either order and on
either child stream. The init hook therefore covers explicit `init`,
config-triggered initialization, and OpenOCD's normal automatic initialization
when `--no-init` is used.

The helper allocates a session loopback address and preflights requested ports
for bind collisions. OpenOCD remains the owner of its enabled GDB, Tcl, telnet,
and RTT listeners. Readiness covers process lifecycle sentinels; it does not
wait for remote service sockets to become connectable. Tcl and telnet are
compatibility endpoints, and their remote socket connectability is not a
readiness condition. Their configured local forwarding processes still start
as part of `RemoteSession.open()`; a local forwarding startup failure prevents
the session from opening. If RTT server startup is part of the sequence,
successful `rtt server start` precedes the startup-complete sentinel, so
readiness follows that command causally.
Generic processes with no required sentinels are ready immediately. The
startup timeout is 30 seconds.

---

## 41. OpenOCD stdout/stderr

Remote OpenOCD output is relayed with bounded low buffering. The helper reads
each child stream in bounded chunks, incrementally decodes UTF-8 with
replacement, omits `LF` delimiters, and emits ordered `CHILD_OUTPUT` fragments
with `line_end` metadata. `line_end` belongs only to `CHILD_OUTPUT` and is true
only when the omitted delimiter was an actual child `LF`. A bounded chunk and
an actual `LF` therefore remain distinct. Long newline-free output becomes
visible before the child exits. Fragment order is preserved within each child
stream. Events from stdout and stderr are serialized in helper-observed order;
no ordering relationship between writes to different child streams is
guaranteed. `SESSION_CLOSED` follows relay completion and is an orderly
session-close event; `ERROR` is a failure event. Both are session-ending events
and are followed by no further event. Readiness matching recognizes each
required sentinel only as a complete trimmed line, so a fragment boundary
cannot make a sentinel appear.

This includes:

- diagnostics;
- flash progress;
- GDB diagnostics;
- RTT diagnostics;
- semihosting console output.

Application console output is not interpreted or rewritten.

---

## 42. Error Handling

Foreground control flow establishes failure precedence. The protocol reader
records facts and wakes waiters; it does not close the session, terminate
forwarding, choose the primary failure, translate helper status into an
OpenOCD status, or perform runner-level error arbitration.

`openocd_returncode` is populated only by the natural OpenOCD termination
event. `check_openocd_exit()` is non-blocking, and
`wait_for_openocd_exit()` waits for that event without implying cleanup.
Forwarding-process health may be checked with bounded local polling while
waiting; no watcher thread or remote/network polling is required.

The first already-established foreground operation failure remains primary.
If no earlier failure exists, a helper, protocol, SSH/control, forwarding, or
required cleanup failure becomes the operation failure. A later OpenOCD result
or session/infrastructure failure is retained as diagnostic information when it
cannot replace the primary failure. The following table defines the required
outcomes:

| Foreground state | Later observation | Primary outcome |
| --- | --- | --- |
| Operation succeeds | No OpenOCD failure; cleanup succeeds | Success |
| Operation succeeds | Cleanup discovers OpenOCD `N != 0`; cleanup succeeds | OpenOCD failure `N` |
| Operation succeeds | Cleanup fails; no OpenOCD failure | Cleanup failure |
| Operation succeeds | Cleanup fails and discovers OpenOCD `N != 0` | Cleanup failure; `N` diagnostic |
| Operation already failed | Cleanup succeeds | Operation failure |
| Operation already failed | Cleanup discovers OpenOCD `N != 0` | Operation failure; `N` diagnostic |
| Operation already failed | Cleanup fails | Operation failure; cleanup diagnostic |
| OpenOCD `N != 0` already foreground-observed | Cleanup subsequently fails | OpenOCD failure `N`; cleanup diagnostic |
| OpenOCD `N != 0` already foreground-observed | Helper/forward failure follows | OpenOCD failure `N`; later failure diagnostic |

### Configuration

Identify the invalid/missing key and configuration path.

### SSH command

If the configured SSH executable cannot be started, identify the configured command.

### SSH authentication/connection

Report failure as an SSH transport error.

### Staging

Do not start OpenOCD with incomplete staging state.

### OpenOCD

Relay diagnostics and propagate failure.

### Local port conflict

Identify the service and port before launching its local client.

### Probe contention

Expose OpenOCD's normal acquisition failure.

### SSH loss

Fail the local operation and clean the remote OpenOCD session.

---

## 43. Code Ownership Boundaries

The physical Python tree in §5 is the current module layout. Logical ownership
is more durable than a duplicate path sketch:

- `zephyr44/runner.py` adapts Zephyr runner state to board-independent plans.
- `remote/openocd_plan.py`, `debug.py`, and `flash.py` construct OpenOCD
  commands without owning their execution.
- `remote/session.py`, `helper_client.py`, and `backend.py` coordinate local
  session lifecycle, helper protocol, and OpenOCD result propagation.
- `remote/model.py` defines service descriptions and `forwarding.py` manages
  local SSH forwards to remote OpenOCD-owned listeners; `remote_helper.py`
  allocates and preflights remote addresses and ports.
- `remote_helper.py` owns remote supervision, output relay, protocol dispatch,
  and cleanup.
- `remote/ssh.py` is the only boundary for configured SSH command behavior.

Platform-specific SSH behavior, if any is eventually needed, shall remain inside the SSH transport layer rather than spread through runner logic.

---

## 44. Test Architecture

The maintained test suite separates self-contained unit tests, local process and
socket integration, Zephyr integration, SSH integration, and destructive
hardware validation. External layers consume explicitly configured environments
and local hardware inventory data. Recording mode remains free of SSH, helper,
OpenOCD, GDB, and hardware I/O. Hardware capabilities are selected
independently so an unsupported optional capability does not suppress other
operations. Acceptance traceability maps criteria to maintained tests; this
document describes only the architecture of that test boundary.

---

## 45. Architecture Decisions

Selected for the current architecture:

- board-agnostic custom runner;
- no board/vendor-specific product behavior;
- Linux supported by one implementation;
- runner name `remote_openocd`;
- built-in `openocd` retained;
- per-user default runner selection;
- two runner identities only;
- `EXTRA_ZEPHYR_MODULES`;
- self-contained Zephyr module;
- no pip/PyPI requirement;
- split Python implementation;
- Python setup script;
- commented configuration template;
- `openocd` default initially;
- CMake configure dependency;
- Zephyr-version-specific reuse of the non-private `OpenOcdBinaryRunner` interface only;
- configurable OpenSSH-compatible client command;
- default SSH command `ssh`;
- SSH command may contain fixed arguments;
- all SSH operations use the configured client abstraction;
- the SSH client continues to use its normal configuration;
- unprivileged remote helper;
- explicit path mappings with staging fallback;
- local GDB;
- remote OpenOCD;
- per-session remote loopback isolation;
- structured RTT handling;
- semihosting console via OpenOCD stdout/stderr;
- one concrete local `RemoteSession` owner;
- one-shot session acquisition through `RemoteSession.open()`;
- no generic `SessionBackend`/`BackendSession` layer;
- cleanup-only `RemoteSession.close()`;
- OpenOCD result stored separately as `openocd_returncode`;
- foreground-controlled error precedence;
- condition-driven session-close synchronization;
- bounded local forwarding-process health polling;
- no persistent artifact cache;
- fail-fast cleanup after SSH loss.

---
