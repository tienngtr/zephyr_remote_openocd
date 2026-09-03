# Zephyr Remote OpenOCD Custom Runner
## Software Architecture Document
### V1 — Product 0.1.0 (initial development)

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

---

# 16. Zephyr Runner Reuse Strategy

V1 may subclass and reuse the non-private interface of Zephyr 4.4's `OpenOcdBinaryRunner` where this materially reduces duplication.

The compatibility policy is:

> `runners.core` is Zephyr's explicitly supported external-runner API. `OpenOcdBinaryRunner` is reusable but is not covered by that compatibility guarantee. Any coupling to its non-private interface is Zephyr-version-specific, remains in the Zephyr compatibility layer, and excludes private attributes and methods.

The Zephyr 4.4 adapter reuses `capabilities()`, `do_add_parser()`, and the
constructor. It overrides `name()`, `do_create()`, and `do_run()`. Constructor
and version coupling is isolated in `zephyr44/runner.py`.

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

The V1 topology uses multiple SSH processes without requiring ControlMaster.

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

The configured SSH command carries arbitrary byte streams, including empty,
textual, binary/NUL-containing, and large payloads, with remote failure
propagation. Production flash uses this transport for session staging.

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
- use of the same configurable abstraction for Linux `ssh` and Windows `ssh.exe`.

WSL-specific validation remains tracked separately in the requirements and
validation documents.

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

The exact frozen Protocol 1 wire contract is maintained in
[protocol-v1.md](protocol-v1.md). This architecture document retains only the
ownership boundary: the helper is versioned, stdout is JSON-lines protocol only,
and incompatible client/helper behavior requires a new protocol version.

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

This combines command completion with socket availability instead of depending
on ordinary human-readable OpenOCD diagnostics.

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

The maintained test suite separates self-contained unit tests, local process and
socket integration, Zephyr integration, SSH integration, destructive hardware
validation, and manual release validation. External layers consume explicitly
configured environments and ignored fixture data. Recording mode remains free of
SSH, helper, OpenOCD, GDB, and hardware I/O. Hardware capabilities are selected
independently so an unsupported optional capability does not suppress other
operations. The separate validation record maps acceptance criteria to executed
evidence; this document describes only the architecture of that test boundary.

---

# 45. Architecture Decisions

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
