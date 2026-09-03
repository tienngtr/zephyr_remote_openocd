# Zephyr Remote OpenOCD Custom Runner
## Software Requirements Specification
### V1 Baseline — Product 0.1.0 (initial development)

# 1. Purpose

This document specifies the requirements for a custom Zephyr west runner which allows developers to use target boards attached to a remote Linux host while keeping their development environment local.

The local development host retains:

- the Zephyr workspace;
- application source;
- build directories;
- compiler and Zephyr SDK;
- GDB;
- west;
- source-level debugging state.

The remote host provides:

- physical debug probes;
- target boards;
- OpenOCD;
- an unprivileged remote helper.

The custom runner integrates with Zephyr without requiring modifications to either the Zephyr source repository or application repositories under development.

The implementation and user-facing documentation SHALL be board- and board-vendor-agnostic.

---

# 2. Terminology

## 2.1 Local host

A supported developer machine running either:

- native Linux; or
- Linux under WSL 2.

Both are first-class V1 platforms.

## 2.2 Remote host

A Linux machine reachable through SSH and physically connected to target boards and debug probes.

## 2.3 Custom runner

The out-of-tree Zephyr `ZephyrBinaryRunner` specified by this document.

Its V1 runner name is:

```text
remote-openocd
```

## 2.4 Built-in OpenOCD runner

Zephyr's existing runner:

```text
openocd
```

## 2.5 Zephyr module

The self-contained out-of-tree directory containing:

- custom runner integration;
- CMake integration;
- Python implementation;
- remote helper;
- configuration template;
- setup tooling.

## 2.6 Distribution / installation

The operation which places the self-contained Zephyr module at a persistent location chosen by the user.

Examples may include extracting a release archive or cloning a source repository.

Distribution does not imply installation as a Python package.

## 2.7 User setup

A separate operation performed after installation which initializes per-user files and provides Zephyr-integration guidance.

## 2.8 SSH command

The OpenSSH-compatible client command selected by the user for remote communication.

On native Linux or WSL 2 this will normally be `ssh` from the local Linux environment.

A WSL 2 user may instead configure a Windows OpenSSH client such as `ssh.exe`.

## 2.9 Remote helper

An unprivileged per-user program executed on the remote host to:

- create and clean session state;
- stage files;
- allocate a session address;
- launch and supervise OpenOCD;
- relay OpenOCD output.

## 2.10 OpenOCD session

One remote OpenOCD process together with its:

- staged files;
- helper process;
- SSH transport;
- remote loopback address;
- local forwarded services.

## 2.11 Probe

A physical hardware debug adapter used by OpenOCD.

## 2.12 Probe channel

An independently usable debug interface exposed by a probe.

A physical probe MAY provide multiple independently usable channels.

---

# 3. Requirement Conventions

## 3.1 Normative language

**SHALL / SHALL NOT**

Mandatory or prohibited behavior for V1 compliance.

**SHOULD / SHOULD NOT**

Recommended behavior which may be violated only for a documented technical reason.

**MAY**

Optional behavior.

Text not using normative language is explanatory unless explicitly stated otherwise.

## 3.2 Rationale

Text introduced by:

```text
Rationale:
```

is non-normative.

## 3.3 Notes

Text introduced by:

```text
Note:
```

is non-normative.

## 3.4 Requirement identifiers

Requirement identifiers are stable and independent of section numbering.

Example:

```text
REQ-FUNC-DEBUG-004
```

Identifiers SHALL NOT be reused.

Removed requirements have their identifiers retired.

Adding or removing requirements SHALL NOT renumber unrelated requirements.

---

# 4. Product Goals

V1 has the following primary goals:

1. Require no source-repository modifications solely for remote-debug integration.
2. Preserve normal Zephyr west workflows.
3. Keep GDB and development artifacts local.
4. Execute OpenOCD remotely.
5. Support concurrent use by multiple developers.
6. Preserve ordinary local OpenOCD operation.
7. Minimize remote-host administration.
8. Reuse applicable Zephyr OpenOCD runner behavior.
9. Keep Zephyr-version-specific integration isolated from the generic remote subsystem.
10. Support native Linux and WSL 2 equally.
11. Avoid imposing a particular SSH-key or SSH-agent arrangement on developers.

---

# 5. Scope

## REQ-FUNC-SCOPE-001

V1 SHALL target Zephyr 4.4.

## REQ-FUNC-SCOPE-002

V1 SHALL support:

```text
west flash
west debug
west attach
west debugserver
west rtt
```

## REQ-FUNC-SCOPE-003

V1 SHALL target normal upstream-compatible OpenOCD behavior expected by Zephyr 4.4.

The requirements SHALL NOT depend on project-specific OpenOCD extensions.

## REQ-FUNC-SCOPE-004

The custom runner SHALL be available only for builds for which the built-in `openocd` runner is available.

## REQ-FUNC-SCOPE-005

The custom runner SHALL NOT automatically advertise remote OpenOCD support for boards which do not support the built-in OpenOCD runner.

## REQ-FUNC-SCOPE-006

Sysbuild and multi-domain flashing are outside V1 scope.

## REQ-FUNC-SCOPE-007

The implementation SHALL NOT contain board- or board-vendor-specific behavior solely to support remote OpenOCD operation.

---

# 6. Source-Repository Independence

## REQ-FUNC-INTEG-001

The custom runner SHALL be installable outside the Zephyr source repository.

## REQ-FUNC-INTEG-002

The custom runner SHALL be installable outside application repositories.

## REQ-FUNC-INTEG-003

Using the custom runner SHALL NOT require creating runner-integration modifications in a Zephyr repository under development.

## REQ-FUNC-INTEG-004

Using the custom runner SHALL NOT require creating runner-integration modifications in an application repository under development.

## REQ-FUNC-INTEG-005

The same installed module SHALL support development of:

- applications contained inside the Zephyr repository;
- independent out-of-tree applications.

## REQ-FUNC-INTEG-006

The custom runner SHALL be discoverable using Zephyr's out-of-tree module and runner mechanisms.

## REQ-FUNC-INTEG-007

The module SHALL support activation through `EXTRA_ZEPHYR_MODULES`.

## REQ-FUNC-INTEG-008

Users MAY choose how they provide `EXTRA_ZEPHYR_MODULES`.

The user documentation SHALL describe at least one convenient mechanism which does not modify a development repository.

---

# 7. Distribution and Setup

## REQ-FUNC-INSTALL-001

V1 SHALL be distributable as a self-contained Zephyr module.

## REQ-FUNC-INSTALL-002

V1 SHALL NOT require installation as a Python package.

## REQ-FUNC-INSTALL-003

V1 SHALL NOT require:

- PyPI;
- a private Python package index;
- pip;
- pipx;
- uv;

for installation or operation.

This prohibits a project-specific package-installation step; it does not
prohibit reuse of dependencies supplied by the supported Zephyr runner
environment, such as `pyelftools` used by the built-in OpenOCD runner.

## REQ-FUNC-INSTALL-004

V1 SHALL be usable when distributed as an ordinary filesystem directory.

Examples MAY include:

- a release archive;
- a source repository checkout.

## REQ-FUNC-INSTALL-005

The module SHALL provide a Python-based user-setup program.

## REQ-FUNC-INSTALL-006

The setup program SHALL execute without installing an additional Python package.

## REQ-FUNC-INSTALL-007

The setup operation SHALL be idempotent.

## REQ-FUNC-INSTALL-008

The persistent module installation directory SHALL NOT be fixed by the requirements.

Documentation SHOULD present a convenient user-local location.

---

# 8. User Configuration

## REQ-FUNC-CONFIG-001

V1 SHALL use:

```text
~/.config/zephyr-remote-openocd/config.toml
```

as its default per-user configuration path on Linux and WSL 2.

## REQ-FUNC-CONFIG-002

The configuration format SHALL be TOML.

## REQ-FUNC-CONFIG-003

The setup operation SHALL create the configuration file if it does not already exist.

## REQ-FUNC-CONFIG-004

The setup operation SHALL NOT overwrite an existing configuration file.

## REQ-FUNC-CONFIG-005

The generated configuration SHALL contain:

- safe defaults;
- explanatory comments;
- commented examples for environment-specific settings.

## REQ-FUNC-CONFIG-006

The generated configuration SHALL preserve local OpenOCD as the default until the user explicitly configures otherwise.

## REQ-FUNC-CONFIG-007

If remote operation is requested while mandatory remote settings are missing, the runner SHALL issue an actionable diagnostic identifying:

- the missing setting;
- the configuration-file location.

## REQ-FUNC-CONFIG-008

Malformed configuration SHALL result in an actionable configuration error rather than an unhandled parser traceback.

## REQ-FUNC-CONFIG-009

Module upgrades SHALL NOT automatically rewrite an existing user configuration merely to add optional settings or comments.

## REQ-FUNC-CONFIG-010

The V1 configuration schema SHALL contain only:

- `zephyr.default`: `"local"` or `"remote"`, default `"local"`;
- `remote.host`: optional non-empty SSH host or alias;
- `remote.openocd`: optional normalized absolute POSIX path;
- `ssh.command`: non-empty string array, default `["ssh"]`;
- `openocd.forward_env`: unique non-empty string array, default `[]`;
- `paths.map`: zero or more tables containing exactly `local` and `remote`.

`remote.host` and `remote.openocd` SHALL be mandatory when a production remote
operation is requested. Local mapping paths SHALL be absolute after optional
home expansion and SHALL be normalized locally. Remote mapping paths SHALL be
normalized absolute POSIX paths.

## REQ-FUNC-CONFIG-011

Unknown keys, disallowed empty strings or command arrays, duplicate environment
names, duplicate mappings, and conflicting mappings SHALL produce actionable
configuration errors.

---

# 9. Runner Availability and Selection

## REQ-FUNC-SELECT-001

For a build which registers `openocd`, module integration SHALL also register:

```text
remote-openocd
```

## REQ-FUNC-SELECT-002

Adding `remote-openocd` SHALL NOT remove the built-in `openocd` runner.

## REQ-FUNC-SELECT-003

The developer SHALL be able to explicitly select local OpenOCD using:

```text
-r openocd
```

## REQ-FUNC-SELECT-004

The developer SHALL be able to explicitly select remote OpenOCD using:

```text
-r remote-openocd
```

## REQ-FUNC-SELECT-005

Per-user configuration SHALL allow the developer to choose whether `openocd` or `remote-openocd` is the default runner for OpenOCD-capable builds.

## REQ-FUNC-SELECT-006

Changing the default-runner preference SHALL NOT require source-repository modification.

## REQ-FUNC-SELECT-007

Explicit `-r` selection SHALL override the generated default.

## REQ-FUNC-SELECT-008

The user configuration SHALL be registered as a CMake configure dependency for builds using the module.

## REQ-FUNC-SELECT-009

When a normal west runner command performs its standard incremental build, a changed default-runner configuration SHOULD cause CMake regeneration before west selects the runner.

## REQ-FUNC-SELECT-010

When rebuilding/reconfiguration is explicitly suppressed, a changed default SHALL NOT be required to affect an existing build until that build is reconfigured.

---

# 10. Board Configuration Reuse

## REQ-FUNC-BOARD-001

The custom runner SHALL reuse common `RunnerConfig` values generated by Zephyr, including applicable:

- board directory;
- ELF path;
- BIN path;
- HEX path;
- GDB path;
- OpenOCD search paths.

## REQ-FUNC-BOARD-002

Applicable board-specific arguments registered for the built-in OpenOCD runner SHALL also be made available to `remote-openocd`.

Representative argument types include:

```text
--cmd-load
--cmd-verify
--file-type
```

and their associated values.

## REQ-FUNC-BOARD-003

Users SHALL NOT need to duplicate built-in OpenOCD board-runner arguments in:

- application source;
- board source;
- user-specific patches.

---

# 11. OpenOCD Runner Compatibility

## REQ-FUNC-OPT-001

The custom runner SHALL support applicable Zephyr 4.4 OpenOCD runner options required by the supported west commands.

## REQ-FUNC-OPT-002

V1 SHALL support probe selection through Zephyr 4.4's `--serial` option.

Rationale:

Remote hosts may contain multiple otherwise equivalent probes.

## REQ-FUNC-OPT-003

If `--serial` is omitted, the custom runner SHALL NOT invent a serial-selection requirement.

## REQ-FUNC-OPT-004

The runner SHALL support distinct:

- remote OpenOCD GDB-server port;
- local GDB-client port;

where Zephyr exposes both.

## REQ-FUNC-OPT-005

The runner SHALL support custom RTT ports supplied through applicable runner options.

## REQ-FUNC-OPT-006

Applicable user-supplied OpenOCD commands SHALL be passed to remote OpenOCD.

## REQ-FUNC-OPT-007

V1 SHALL NOT be required to translate arbitrary local paths embedded in arbitrary user-written Tcl.

---

# 12. Zephyr OpenOCD Runner Reuse Boundary

## REQ-NFUNC-COMPAT-001

The Zephyr 4.4 adapter MAY subclass `OpenOcdBinaryRunner` and reuse its non-private interface where this materially reduces duplication.

## REQ-NFUNC-COMPAT-002

Because `OpenOcdBinaryRunner` is not part of Zephyr's explicit external-runner compatibility guarantee, all such coupling SHALL be treated as Zephyr-version-specific and confined to the Zephyr compatibility layer.

## REQ-NFUNC-COMPAT-003

The custom runner SHALL NOT depend on private attributes or private methods of `OpenOcdBinaryRunner`.

## REQ-NFUNC-COMPAT-004

Supporting a new Zephyr version SHALL require either validation of the existing adapter or a version-specific adapter update.

---

# 13. Runtime Environment Forwarding

## REQ-FUNC-ENV-001

The runner SHALL support an explicit allow-list of local environment-variable names forwarded to remote OpenOCD.

## REQ-FUNC-ENV-002

The complete local environment SHALL NOT be forwarded implicitly.

## REQ-FUNC-ENV-003

Forwarded variables SHALL be available to remote OpenOCD before it processes configuration files.

Rationale:

Runtime values may influence probe, adapter, or target configuration while OpenOCD configuration files are being evaluated.

## REQ-FUNC-ENV-004

If an allow-listed variable is absent locally, the runner SHALL:

1. emit a non-fatal warning;
2. omit the variable remotely;
3. continue execution.

---

# 14. Remote Host and OpenOCD

## REQ-FUNC-REMOTE-001

OpenOCD SHALL execute on a configured remote Linux host.

## REQ-FUNC-REMOTE-002

The remote OpenOCD executable SHALL be configurable per user.

## REQ-FUNC-REMOTE-003

The remote OpenOCD executable SHALL NOT be required to exist in the remote user's `PATH`.

## REQ-FUNC-REMOTE-004

The local development host SHALL retain:

- GDB;
- source files;
- debug symbols;
- local build output used by development tools;
- the compiler/toolchain.

---

# 15. OpenOCD Configuration and Files

## REQ-FUNC-FILE-001

Files directly required by remote OpenOCD SHALL be accessible on the remote host.

## REQ-FUNC-FILE-002

Board-specific OpenOCD configuration from the developer's local Zephyr tree SHALL remain usable remotely.

## REQ-FUNC-FILE-003

Board OpenOCD configuration SHALL retain the ability to source common configuration files when the equivalent local OpenOCD setup can resolve them.

## REQ-FUNC-FILE-004

The runner SHALL support explicit recursive local-to-remote path mappings.

## REQ-FUNC-FILE-005

A required local file not covered by an explicit mapping SHALL be staged into the current remote session.

## REQ-FUNC-FILE-006

A required local search directory not covered by an explicit mapping SHALL be staged while preserving relative structure required by OpenOCD lookup.

## REQ-FUNC-FILE-007

An OpenOCD search path supplied by the Zephyr build SHALL NOT be silently discarded merely because current board configuration is believed not to use it.

## REQ-FUNC-FILE-008

V1 SHALL NOT maintain a persistent cross-session firmware or configuration cache.

---

# 16. Flash

## REQ-FUNC-FLASH-001

`west flash -r remote-openocd` SHALL program the intended remote target.

## REQ-FUNC-FLASH-002

Firmware directly required by remote OpenOCD SHALL be staged when not available through a configured mapping.

## REQ-FUNC-FLASH-003

Commands executed by remote OpenOCD SHALL reference remote paths for remotely accessed firmware.

## REQ-FUNC-FLASH-004

Applicable OpenOCD flash behavior SHALL be preserved, including:

- erase;
- load;
- verification;
- verification-only;
- supported firmware file types;
- supported custom OpenOCD commands.

## REQ-FUNC-FLASH-005

A failed remote OpenOCD flash operation SHALL cause the west operation to fail.

---

# 17. Debug and Attach

## REQ-FUNC-DEBUG-001

GDB SHALL execute locally.

## REQ-FUNC-DEBUG-002

The OpenOCD GDB server SHALL execute remotely.

## REQ-FUNC-DEBUG-003

The custom runner SHALL establish required local-to-remote GDB transport before launching local GDB.

## REQ-FUNC-DEBUG-004

`west debug -r remote-openocd` SHALL support normal source-level debugging including:

- program loading;
- halt;
- resume;
- memory inspection;
- register inspection;
- breakpoints.

## REQ-FUNC-DEBUG-005

`west attach -r remote-openocd` SHALL attach local GDB without flashing solely because the target is remote.

## REQ-FUNC-DEBUG-006

`west debugserver -r remote-openocd` SHALL expose a locally reachable GDB-server endpoint backed by remote OpenOCD.

## REQ-FUNC-DEBUG-007

Probe-selection information available through the structured runner interface SHOULD behave consistently across supported debug operations.

---

# 18. OpenOCD Network Services

## REQ-FUNC-SVC-001

The custom runner SHALL locally expose required enabled remote OpenOCD services.

Relevant services include:

- GDB;
- Tcl;
- telnet;
- RTT.

## REQ-FUNC-SVC-002

A disabled OpenOCD service SHALL NOT require a corresponding local listener.

## REQ-FUNC-SVC-003

Local forwarded services SHALL bind only to local loopback interfaces.

## REQ-FUNC-SVC-004

Remote OpenOCD services created for a remote-runner session SHALL bind only to remote loopback addresses.

## REQ-FUNC-SVC-005

If a required local service port is occupied, the operation SHALL fail rather than silently choose another port.

## REQ-FUNC-SVC-006

A local-port conflict SHALL identify the affected service and port.

---

# 19. RTT

## REQ-FUNC-RTT-001

V1 SHALL support RTT channel 0.

## REQ-FUNC-RTT-002

RTT SHALL support bidirectional communication.

## REQ-FUNC-RTT-003

`west rtt -r remote-openocd` SHALL configure RTT using remote OpenOCD and launch the local RTT client.

## REQ-FUNC-RTT-004

Custom `--rtt-port` values SHALL be supported.

## REQ-FUNC-RTT-005

`west debug -r remote-openocd --rtt-server` SHALL be supported.

## REQ-FUNC-RTT-006

`west debugserver -r remote-openocd --rtt-server` SHALL be supported where the corresponding runner operation supports RTT.

## REQ-FUNC-RTT-007

V1 SHALL NOT require GDB Remote Serial Protocol inspection solely to determine RTT configuration.

---

# 20. Semihosting Console

## REQ-FUNC-SEMI-001

Ordinary OpenOCD commands used to enable semihosting SHALL be accepted through applicable runner command options.

## REQ-FUNC-SEMI-002

Semihosting console output emitted by remote OpenOCD on stdout/stderr SHALL appear in the local west terminal.

## REQ-FUNC-SEMI-003

V1 SHALL NOT require a dedicated semihosting network protocol or proxy.

---

# 21. SSH Client Selection and Compatibility

## REQ-FUNC-SSH-001

The runner SHALL use an OpenSSH-compatible external client command for SSH transport.

## REQ-FUNC-SSH-002

The default SSH command SHALL use `ssh` resolved from the local host's normal command search path.

## REQ-FUNC-SSH-003

The per-user configuration SHALL allow the SSH command to be overridden.

Rationale:

A WSL 2 user may prefer Windows `ssh.exe` so that the runner uses the user's existing Windows OpenSSH configuration, credentials, or SSH agent rather than requiring a separate WSL-side SSH setup.

## REQ-FUNC-SSH-004

The SSH command override SHALL permit selection of:

- a different executable; and
- fixed command-line arguments required for that executable.

## REQ-FUNC-SSH-005

The configured SSH command SHALL be used consistently for remote-runner SSH operations.

## REQ-FUNC-SSH-006

The runner SHALL remain compatible with normal features provided by the selected OpenSSH client, including where supported:

- host aliases;
- public-key authentication;
- SSH agents;
- interactive authentication;
- ProxyJump.

## REQ-FUNC-SSH-007

The runner SHALL NOT require users to duplicate normal SSH credentials, keys, or proxy configuration in the remote-runner configuration.

## REQ-FUNC-SSH-008

The correctness of V1 SHALL NOT depend on SSH ControlMaster or client connection multiplexing.

## REQ-FUNC-SSH-009

The implementation MAY use client connection multiplexing when the configured SSH client supports it.

## REQ-FUNC-SSH-010

Loss of the controlling SSH session SHALL cause the corresponding remote OpenOCD session to terminate.

## REQ-FUNC-SSH-011

V1 SHALL NOT attempt transparent reconstruction of an interrupted debugging session after SSH loss.

---

# 22. Concurrent Users and Probe Contention

## REQ-FUNC-CONC-001

Multiple developers SHALL be able to operate independent remote OpenOCD sessions concurrently.

## REQ-FUNC-CONC-002

Independently usable channels on the same physical probe MAY be used concurrently.

## REQ-FUNC-CONC-003

V1 SHALL NOT implement an additional board reservation service.

## REQ-FUNC-CONC-004

If OpenOCD cannot acquire the requested probe or channel because another process owns it, the later operation SHALL fail rather than be queued.

---

# 23. Remote Helper

## REQ-FUNC-HELP-001

Routine helper installation and execution SHALL NOT require root privileges.

## REQ-FUNC-HELP-002

A compatible helper SHALL be automatically deployable to the remote user's account.

## REQ-FUNC-HELP-003

V1 SHALL NOT require a persistent privileged or system-wide daemon.

## REQ-FUNC-HELP-004

The helper SHALL supervise remote OpenOCD.

## REQ-FUNC-HELP-005

Loss of the controlling helper/SSH session SHALL cause the associated OpenOCD process to terminate.

## REQ-FUNC-HELP-006

Helper protocol compatibility SHALL be versioned. Protocol 1 SHALL implement
the frozen Protocol 1 helper contract. Any incompatible change, or new client
behavior requiring helper support not guaranteed by that contract, SHALL use a
new protocol version.

## REQ-FUNC-HELP-007

Obsolete helper versions SHALL NOT accumulate indefinitely. Deployment SHALL
replace the helper for its selected protocol path atomically.

---

# 24. Session Data

## REQ-FUNC-DATA-001

Each developer SHALL use a separate remote Unix account.

## REQ-FUNC-DATA-002

Remote session files SHALL be protected from other ordinary remote users by filesystem permissions.

## REQ-FUNC-DATA-003

Normal session termination SHALL remove temporary session artifacts.

## REQ-FUNC-DATA-004

Persistent fallback session state older than 24 hours MAY be deleted opportunistically.

## REQ-FUNC-DATA-005

Repeated use SHALL NOT cause unbounded accumulation of abandoned session data.

---

# 25. Platform Requirements

## REQ-NFUNC-PLAT-001

V1 SHALL support native Linux and WSL 2 as first-class local development platforms.

Neither platform SHALL be treated as a secondary or future port.

## REQ-NFUNC-PLAT-002

Features required for normal remote-runner operation SHOULD behave equivalently on native Linux and WSL 2.

## REQ-NFUNC-PLAT-003

The remote platform SHALL be Linux with Python 3.12 or newer.

## REQ-NFUNC-PLAT-004

The local host SHALL provide Python 3.12 or newer.

## REQ-NFUNC-PORT-001

The Python setup program and generic runner implementation SHOULD avoid unnecessary Linux-kernel-specific assumptions which would hinder a future native Windows port.

Native Windows execution itself remains outside V1 scope.

---

# 26. Other Non-Functional Requirements

## REQ-NFUNC-MAINT-001

The custom runner implementation SHOULD use Python 3.12 or newer.

## REQ-NFUNC-MAINT-002

Runtime implementation SHOULD minimize additional third-party Python
dependencies. Dependencies already required by the supported Zephyr runner
environment MAY be reused where appropriate.

## REQ-NFUNC-MAINT-003

The implementation SHOULD separate:

- Zephyr-version compatibility;
- configuration;
- OpenOCD command construction;
- staging;
- SSH transport;
- local service forwarding;
- helper protocol;
- process supervision;
- diagnostics.

## REQ-NFUNC-PERF-001

Under representative local conditions, each invocation of `remote-openocd` SHOULD introduce less than 0.5 seconds of additional runner-controlled startup processing compared with the equivalent invocation of Zephyr's built-in `openocd` runner.

SSH authentication, external network latency, network-transfer time, and remote OpenOCD initialization are excluded.

## REQ-NFUNC-ADMIN-001

Routine use, helper deployment, upgrades, cleanup, and diagnostics SHALL NOT require root privileges.

## REQ-NFUNC-TEST-001

Runner option and OpenOCD command construction SHALL be testable without physical hardware.

## REQ-NFUNC-TEST-002

Remote-session and forwarding behavior SHALL be testable using fake OpenOCD endpoints.

## REQ-NFUNC-TEST-003

Automated integration tests SHALL exercise both native Linux and WSL 2 where practical.

---

# 27. Assumptions

## ASM-001

Developers have ordinary SSH access to the remote host.

## ASM-002

Developers use separate remote Unix accounts.

## ASM-003

Lab users are trusted.

## ASM-004

OpenOCD/debug-probe acquisition provides acceptable exclusion when a probe/channel is occupied.

## ASM-005

Independent probe channels supported by the underlying hardware and OpenOCD may be controlled by independent OpenOCD processes.

## ASM-006

The remote Linux host can run multiple OpenOCD instances using identical TCP port numbers when bound to different loopback addresses.


## ASM-007

Firmware and configuration artifacts are sufficiently small that a persistent artifact cache is unnecessary for V1.

---

# 28. Explicit Non-Goals

V1 does not include:

- board-specific remote-runner implementations;
- source changes solely for remote-runner integration;
- pip/PyPI-based installation requirements;
- arbitrary direct remote OpenOCD invocation;
- arbitrary Tcl filesystem virtualization;
- remote compilation;
- remote GDB;
- automatic board reservation;
- automatic lab-host discovery;
- transparent debugging recovery after SSH loss;
- native Windows execution;
- non-OpenOCD debug servers;
- sysbuild or multi-domain flashing;
- multiple simultaneous RTT channels;
- multiple simultaneous RTT clients;
- semihosting filesystem virtualization;
- GDB File-I/O remoting for semihosting;
- requiring a specific SSH-agent implementation;
- requiring SSH ControlMaster.

---

# 29. Major Risks

## RISK-003 — Zephyr-version API coupling

Zephyr explicitly guarantees `runners.core` as its external-runner API, but does not extend that guarantee to `OpenOcdBinaryRunner`. Reusing the latter's non-private interface therefore creates version-specific maintenance coupling.

Severity: Medium.

Mitigation:

Confine all `OpenOcdBinaryRunner` coupling to the Zephyr 4.4 compatibility layer, use no private attributes or methods, and validate or update the adapter for each newly supported Zephyr version.

## RISK-007 — SSH client differences, especially WSL with Windows `ssh.exe`

WSL Linux OpenSSH and Windows `ssh.exe` invoked from WSL may differ in subprocess, path, authentication, or forwarding behavior and require separate validation

Severity: Medium.

Mitigation:

Base V1 correctness only on required OpenSSH-compatible behavior, keep multiplexing optional, and complete PG-012 and PG-013 before claiming WSL 2 compatibility.


# 30. Acceptance Criteria

## AC-INTEG-001

An in-tree Zephyr application can use the custom runner without runner-related source-tree modifications.

## AC-INTEG-002

An independent out-of-tree application can use the same installed runner without application-repository modifications.

## AC-INTEG-003

For an OpenOCD-capable build, generated runner state contains both:

```text
openocd
remote-openocd
```

without board-source modification.

## AC-INTEG-004

The custom runner receives equivalent applicable runner-specific arguments to the built-in OpenOCD runner.

## AC-INTEG-005

`-r openocd` continues to work after module integration.

## AC-INSTALL-001

The module can be installed from a plain filesystem copy/archive without Python packaging infrastructure.

## AC-INSTALL-002

The Python setup program creates the default configuration when absent.

## AC-INSTALL-003

Repeated setup leaves an existing configuration intact.

## AC-INSTALL-004

Setup reports whether configuration was created or reused, its absolute path,
the module root, and concise `EXTRA_ZEPHYR_MODULES` activation guidance.

## AC-INSTALL-005

Setup creates the configuration directory with mode `0700` and the
configuration file with mode `0600`, without changing permissions on any
pre-existing parent, directory, or file.

## AC-INSTALL-006

Setup reports whether `pyelftools` is discoverable in the active Python
environment. A missing dependency produces a warning but does not prevent
configuration initialization.

## AC-SELECT-001

A build configured with local default selects `openocd` when no explicit runner is supplied.

## AC-SELECT-002

A build configured with remote default selects `remote-openocd` when no explicit runner is supplied.

## AC-SELECT-003

Changing the configuration default followed by a normal west runner invocation which performs the standard incremental build updates the generated default without requiring a pristine build.


## AC-FLASH-001

`west flash -r remote-openocd` programs the intended remote target.


## AC-DEBUG-001

`west debug -r remote-openocd` provides local source-level debugging through remote OpenOCD.


## AC-DEBUG-002

Different GDB server/client ports work correctly when supported by the runner interface.


## AC-RTT-001

`west rtt -r remote-openocd` provides bidirectional RTT channel-0 operation.

## AC-RTT-002

A custom RTT port works without inspecting GDB RSP traffic.

## AC-SEMI-001

Semihosting console output emitted by remote OpenOCD appears in the local west terminal.


## AC-CONC-001

Independent remote target sessions can coexist without OpenOCD service-port collisions.

## AC-LIFE-001

Normal termination removes the corresponding remote OpenOCD process and temporary session state.

## AC-LIFE-002

Loss of the controlling SSH session terminates the corresponding remote OpenOCD process.

## AC-PLAT-001

The normal flash/debug workflow works on native Linux.

## AC-PLAT-002

The normal flash/debug workflow works under WSL 2.

## AC-SSH-001

With no SSH override configured, the runner uses `ssh` from the local command search path.

## AC-SSH-002

On WSL 2, configuring a Windows OpenSSH client executable as the SSH command allows the runner to perform remote operations without requiring use of the WSL distribution's `ssh` executable.

## AC-SSH-003

An SSH command containing fixed user-configured arguments can be used without those arguments needing to be duplicated by every operation.
