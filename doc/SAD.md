# Zephyr Remote OpenOCD Custom Runner
## Software Architecture Document
### V1 — Draft 0.4

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

Provisional structure:

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

    remote_helper/
        main.py
        session.py
        openocd.py
        cleanup.py

    resources/
        config.toml.example

    scripts/
        setup.py
```

Exact filenames are not architectural contracts.

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

No pip installation is required.

---

# 7. Distribution and User Setup

Distribution places the complete module at an arbitrary persistent path.

User setup is a separate operation.

A typical documented flow may be:

```text
1. Obtain the module.
2. Place it at a persistent path.
3. Run scripts/setup.py.
4. Edit ~/.config/zephyr-remote-openocd/config.toml.
5. Add the module path to EXTRA_ZEPHYR_MODULES using the user's preferred mechanism.
```

`setup.py` is Python rather than a shell script to reduce platform-specific setup logic.

---

# 8. Configuration Template

Representative configuration:

```toml
# Zephyr Remote OpenOCD

[zephyr]

# "local"  -> built-in openocd
# "remote" -> remote-openocd
default = "local"


[remote]

# SSH host or alias understood by the selected SSH client.
#
# host = "board-lab"

# Absolute OpenOCD path on the remote host.
#
# openocd = "/home/user/openocd/bin/openocd"


[ssh]

# OpenSSH-compatible client command.
#
# Default:
command = ["ssh"]

# WSL 2 example using Windows OpenSSH:
#
# command = ["/mnt/c/Windows/System32/OpenSSH/ssh.exe"]

# Fixed arguments can be included if required:
#
# command = ["ssh", "-F", "/home/user/.ssh/lab_config"]


[openocd]

# Local environment variables forwarded to remote OpenOCD.
forward_env = []


# [[paths.map]]
# local = "/local/openocd/scripts"
# remote = "/remote/openocd/scripts"
```

The SSH command is represented provisionally as an argv list rather than a shell command string.

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
establish GDB transport
       |
establish RTT transport
       |
run local batch GDB
       |
       +-- RTT setup
       +-- RTT start
       +-- RTT server start <port>
       |
launch local RTT client
```

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

PG-015 validated arbitrary byte streaming through the configured native-Linux SSH command, including empty, textual, binary/NUL-containing, and 1 MiB payloads, with remote failure propagation. This establishes the transport primitive only.

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

The staging manifest, archive encoding and extraction, remote filesystem layout, path rewriting, helper deployment protocol, and real OpenOCD artifact staging remain implementation decisions.

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

The protocol is versioned.

Logical operations may include:

```text
HELLO
CREATE_SESSION
STAGE
START_OPENOCD
PROCESS_EVENT
STOP
```

The protocol communicates:

- compatibility version;
- session ID;
- loopback address;
- staging results;
- process state;
- exit status;
- termination reason.

Bulk binary content should use stream-oriented transfer rather than JSON/base64 where practical.

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

Preferred readiness mechanisms should probe service availability rather than parse human-readable OpenOCD logs.

Exact implementation remains a prototype decision.

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

Without that explicit test setting, the unimplemented runner fails clearly. These tests use a harmless OpenOCD stub and do not access physical hardware or execute real OpenOCD behavior.

## 44.3 SSH integration

SSH integration tests cover native-Linux command selection, fixed arguments, streaming, remote failure propagation, and separate controller/forwarding processes without ControlMaster.

PG-012 and PG-013 remain present as permanent WSL-specific tests. They skip explicitly outside WSL 2 rather than being treated as passed.

## 44.4 Later hardware integration

Hardware test fixtures may use concrete boards, but product code and general documentation remain board-agnostic.

Later real OpenOCD validation SHOULD include at least two OpenOCD-capable target configurations with materially different board/SoC characteristics.

At least one validation target SHOULD exercise RTT.

At least one validation target SHOULD exercise semihosting console output.

## 44.5 Platform matrix

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

The Windows-client case validates that the SSH command abstraction does not depend on the WSL distribution's credentials or agent. Full real OpenOCD workflows on both native Linux and WSL 2 remain later hardware-integration work.

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

The permanent native-Linux suite contains 18 tests: 16 pass and the two WSL-specific tests for PG-012 and PG-013 skip explicitly.

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

The remaining implementation-level questions are:

1. Exact distribution instructions.
2. Final setup-script invocation/name.
3. Exact helper protocol encoding.
4. Whether optional multiplexing is worthwhile when available.
5. Exact staging archive format.
6. Exact remote-service readiness mechanism.
7. Loopback collision retry parameters.
8. Final internal Python module layout beyond the validated compatibility boundary.

None currently require an additional product decision before the next implementation phase.

---

# 49. Phase Transition

```text
Zephyr integration prototype
    COMPLETE except deferred WSL-specific PG-012 and PG-013

Permanent regression transformation
    COMPLETE

Next: remote-session vertical slice
    configuration and setup completion
    helper protocol and deployment
    session lifecycle
    staging and path translation
    loopback allocation
    SSH forwarding integration
    fake-backend lifecycle tests

Then
    real OpenOCD flash
    debug, attach, and debugserver
    RTT
    semihosting console
```
