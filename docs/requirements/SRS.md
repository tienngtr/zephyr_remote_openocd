# Zephyr Remote OpenOCD Software Requirements Specification

## Navigation

- [Context, terminology, and scope](#1-purpose)
- [Integration and configuration](#6-source-repository-independence)
- [Remote execution and user operations](#13-remote-host-and-openocd)
- [Lifecycle, platform, and quality requirements](#21-concurrent-users-and-probe-contention)
- [Assumptions, risks, and non-goals](#26-assumptions)
- [Verification traceability](../traceability/verification.md)

## 1. Purpose

This document specifies a Zephyr west runner for boards attached to a remote
Linux host. The Zephyr workspace and development tools remain local.

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

The runner integrates without modifying the Zephyr source tree or the
application source tree.

The implementation and user-facing documentation is board- and board-vendor-agnostic.

---

## 2. Terminology

### 2.1 Local host

A supported developer machine running Linux with Python 3.12 or newer.

### 2.2 Remote host

A Linux machine reachable through SSH and physically connected to target boards and debug probes.

### 2.3 Custom runner

The out-of-tree Zephyr `ZephyrBinaryRunner` specified by this document.

Its runner name is:

```text
remote_openocd
```

### 2.4 Built-in OpenOCD runner

Zephyr's existing runner:

```text
openocd
```

### 2.5 Zephyr module

The self-contained out-of-tree directory containing:

- custom runner integration;
- CMake integration;
- Python implementation;
- remote helper;
- configuration template;
- setup tooling.

### 2.6 Distribution / installation

The operation which places the self-contained Zephyr module at a persistent location chosen by the user.

Examples may include extracting a release archive or cloning a source repository.

Distribution does not imply installation as a Python package.

### 2.7 User setup

A separate operation performed after installation which initializes per-user files and provides Zephyr-integration guidance.

### 2.8 SSH command

The OpenSSH-compatible client command selected by the user for remote communication.

This will normally be `ssh` found through `PATH`, but configuration may name
another OpenSSH-compatible executable and fixed arguments.

### 2.9 Remote helper

An unprivileged per-user program executed on the remote host to:

- create and clean remote-session state;
- stage files;
- allocate a session address;
- launch and supervise OpenOCD;
- relay OpenOCD output.

### 2.10 Remote session

The runner-managed lifetime containing:

- the remote helper and its control channel;
- the staged files and remote workspace;
- the OpenOCD process;
- the SSH transport;
- the allocated remote loopback address;
- the local forwarded services.

### 2.11 Helper control channel

The protocol connection between the local runner and the remote helper. It
carries session commands, OpenOCD output events, startup status, and the final
session result.

### 2.12 Probe

A physical debug adapter used by OpenOCD.

### 2.13 Probe channel

An independently usable debug interface exposed by a probe.

A physical probe can provide multiple independently usable channels.

---

## 3. Requirement Conventions

### 3.1 Normative language

**SHALL / SHALL NOT**

Mandatory or prohibited behavior for conformance to this specification.

**SHOULD / SHOULD NOT**

Recommended behavior which may be violated only for a documented technical reason.

**MAY**

Optional behavior.

Text not using normative language is explanatory unless explicitly stated otherwise.

### 3.2 Rationale

Text introduced by:

```text
Rationale:
```

is non-normative.

### 3.3 Notes

Text introduced by:

```text
Note:
```

is non-normative.

### 3.4 Requirement identifiers

Requirement identifiers are stable and independent of section numbering.

Example:

```text
REQ-FUNC-<DOMAIN>-<NNN>
```

Identifiers SHALL NOT be reused.

Removed requirements have their identifiers retired.

Adding or removing requirements SHALL NOT renumber unrelated requirements.

---

## 4. Product Goals

The project has the following primary goals:

1. Require no source-repository modifications solely for remote-debug integration.
2. Preserve normal Zephyr west workflows.
3. Keep GDB and development artifacts local.
4. Execute OpenOCD remotely.
5. Support concurrent use by multiple developers.
6. Preserve ordinary local OpenOCD operation.
7. Minimize remote-host administration.
8. Reuse the Zephyr 4.4 OpenOCD runner's user-facing interface while defining
   remote-specific behavior explicitly in this specification.
9. Keep Zephyr-version-specific integration isolated from the generic remote subsystem.
10. Support Linux as the local platform.
11. Avoid imposing a particular SSH-key or SSH-agent arrangement on developers.

---

## 5. Scope

### REQ-FUNC-SCOPE-001

The runner SHALL target Zephyr 4.4.

### REQ-FUNC-SCOPE-002

The runner SHALL support:

```text
west flash
west debug
west attach
west debugserver
west rtt
```

### REQ-FUNC-SCOPE-003

Supported runner operations SHALL NOT require project-specific OpenOCD
extensions.

### REQ-FUNC-SCOPE-004

The custom runner SHALL be available only for builds for which the built-in `openocd` runner is available.

### REQ-FUNC-SCOPE-005

The custom runner SHALL NOT automatically advertise remote OpenOCD support for boards which do not support the built-in OpenOCD runner.

### REQ-FUNC-SCOPE-007

The implementation SHALL NOT contain board- or board-vendor-specific behavior solely to support remote OpenOCD operation.

---

## 6. Source-Repository Independence

### REQ-FUNC-INTEG-001

The custom runner SHALL be installable outside the Zephyr source repository.

### REQ-FUNC-INTEG-002

The custom runner SHALL be installable outside application repositories.

### REQ-FUNC-INTEG-003

Using the custom runner SHALL NOT require creating runner-integration modifications in a Zephyr repository under development.

### REQ-FUNC-INTEG-004

Using the custom runner SHALL NOT require creating runner-integration modifications in an application repository under development.

### REQ-FUNC-INTEG-005

The same installed module SHALL support development of:

- applications contained inside the Zephyr repository;
- independent out-of-tree applications.

### REQ-FUNC-INTEG-006

The custom runner SHALL be discoverable using Zephyr's out-of-tree module and runner mechanisms.

### REQ-FUNC-INTEG-007

The module SHALL support activation through `EXTRA_ZEPHYR_MODULES`.

### REQ-FUNC-INTEG-008

Users MAY choose how they provide `EXTRA_ZEPHYR_MODULES`.

The user documentation SHALL describe at least one mechanism for providing
`EXTRA_ZEPHYR_MODULES` which does not modify a development repository.

---

## 7. Distribution and Setup

### REQ-FUNC-INSTALL-001

The project SHALL be distributable as a self-contained Zephyr module.

### REQ-FUNC-INSTALL-002

The project SHALL NOT require installation as a Python package.

### REQ-FUNC-INSTALL-003

The project SHALL NOT require:

- PyPI;
- a private Python package index;
- pip;
- pipx;
- uv;

for installation or operation.

This prohibits a project-specific package-installation step; it does not
prohibit reuse of dependencies supplied by the supported Zephyr 4.4 runner
environment, including `pyelftools`, PyYAML, and jsonschema.

### REQ-FUNC-INSTALL-004

The project SHALL be usable when distributed as an ordinary filesystem directory.

Examples MAY include:

- a release archive;
- a source repository checkout.

### REQ-FUNC-INSTALL-005

The module SHALL provide a Python-based user-setup program.

### REQ-FUNC-INSTALL-006

The setup program SHALL execute without installing an additional Python package.

### REQ-FUNC-INSTALL-007

The setup operation SHALL be idempotent.

### REQ-FUNC-INSTALL-008

The module SHALL NOT require installation at a fixed filesystem path.

### REQ-FUNC-INSTALL-009

The setup program SHALL report whether configuration was created or reused, its
absolute path, the module root, and concise `EXTRA_ZEPHYR_MODULES` activation
guidance.

### REQ-FUNC-INSTALL-010

The setup program SHALL create the configuration directory with mode `0700` and
the configuration file with mode `0600`, without changing permissions on any
pre-existing parent, directory, or file.

### REQ-FUNC-INSTALL-011

The setup program SHALL report whether `pyelftools`, PyYAML, and jsonschema are
discoverable in the active Python environment. A missing dependency SHALL
produce a warning directing the user to the Zephyr 4.4-configured Python
environment, but SHALL NOT prevent configuration initialization or recommend a
separate product installation.

---

## 8. User Configuration

### REQ-FUNC-CONFIG-001

The implementation SHALL use:

```text
~/.config/zephyr_remote_openocd/config.yaml
```

as its default per-user configuration path on Linux.

### REQ-FUNC-CONFIG-002

The configuration format SHALL be YAML. The enforceable data contract is
[`configuration.schema.json`](../../python/zephyr_remote_openocd/resources/configuration.schema.json);
YAML is parsed
safely with duplicate-key rejection before schema validation.

### REQ-FUNC-CONFIG-003

The setup operation SHALL create the configuration file if it does not already exist.

### REQ-FUNC-CONFIG-004

The setup operation SHALL NOT overwrite an existing configuration file.

### REQ-FUNC-CONFIG-005

The generated configuration SHALL contain:

- safe defaults;
- explanatory comments;
- a schema-valid but unselected placeholder remote;
- every direct remote setting needed as a starting point;
- commented examples for default selection, presets, and environment-specific
  settings.

### REQ-FUNC-CONFIG-006

The generated configuration SHALL preserve local OpenOCD as the default until the user explicitly configures otherwise.

### REQ-FUNC-CONFIG-007

If remote operation is requested while mandatory remote settings are missing, the runner SHALL issue an actionable diagnostic identifying:

- the missing setting;
- the configuration-file location.

### REQ-FUNC-CONFIG-008

An absent configured path SHALL be treated as empty configuration. An existing
empty or comment-only YAML document SHALL also be accepted as the empty mapping
`{}` and validated against the canonical schema. A configured path that exists
but cannot be read as a file, or a document that fails YAML parsing, document
or root validation, or schema validation, SHALL result in an actionable
configuration error rather than silently using empty configuration or defaults
or exposing an unhandled parser traceback.

### REQ-FUNC-CONFIG-009

Module upgrades SHALL NOT automatically rewrite an existing user configuration merely to add optional settings or comments.

### REQ-FUNC-CONFIG-011

Unknown keys, explicit nulls, duplicate YAML keys, invalid types, disallowed
empty command executables, SSH hosts containing NUL, duplicate environment
names, invalid paths, duplicate mappings, and conflicting mappings SHALL
produce actionable configuration errors.

For path mappings, a duplicate mapping has a local key that normalizes to an
already defined local root with the same destination. A conflicting mapping has
the same normalized local root with a different destination. Distinct
normalized local roots in an ancestor/descendant relationship are overlapping
mappings and SHALL NOT be treated as conflicts.

### REQ-FUNC-CONFIG-012

Configuration documents SHALL be accepted or rejected according to the
canonical machine-readable schema for every structural and lexical rule that
the schema expresses. The implementation SHALL additionally enforce the
contextual semantics required by this specification where the schema cannot
express them, including local path resolution and collision detection,
selected-definition references, operationally required settings, and
remote-home expansion.

### REQ-FUNC-CONFIG-013

The module SHALL provide a user-facing command that validates an existing
configuration and summarizes its effective settings without SSH,
OpenOCD, GDB, subprocess, socket, or hardware operations.

For this summary, the omitted data is limited to the current local values of
environment variables named by `forward_env`; the command SHALL NOT read or
print those values, but MAY print their names. The summary SHALL include
configured SSH and OpenOCD argv elements, hosts, path mappings, and selected
definition names as diagnostic content. It SHALL NOT infer or redact secrets
from command arguments or paths; configured command arguments MAY therefore
contain values that are sensitive to the user.

The command SHALL resolve an explicitly named remote, otherwise
`default_remote`, while deliberately ignoring
`ZEPHYR_REMOTE_OPENOCD_REMOTE`. When neither is selected, it SHALL report
structural success and the available definitions. A missing target file SHALL
be an actionable validation failure rather than being treated as empty runtime
configuration.

### REQ-FUNC-CONFIG-014

A non-empty `ZEPHYR_REMOTE_OPENOCD_CONFIG` environment variable SHALL override
the default configuration path. A leading current-user `~` in the override
SHALL be expanded before the configuration is read.

### REQ-FUNC-CONFIG-015

The top level SHALL contain only `default_runner`, `default_remote`, `presets`,
and `remotes`. Presets and remotes SHALL use the fields and strict types in the
canonical JSON Schema. Commands SHALL be argv arrays; environment forwarding
and path mappings SHALL use the YAML forms specified by the schema.

### REQ-FUNC-CONFIG-016

Production remote selection SHALL use `--remote`, then a non-empty
`ZEPHYR_REMOTE_OPENOCD_REMOTE`, then `default_remote`.

### REQ-FUNC-CONFIG-017

A selected remote MAY reference one preset. Explicit remote settings SHALL
replace preset settings in full; lists and mappings SHALL NOT be merged.

### REQ-FUNC-CONFIG-018

Built-in defaults SHALL be `ssh_command: [ssh]`, empty `forward_env`, and empty
`path_mappings`. When `ssh_host` is omitted, it SHALL default to the selected
remote name.

### REQ-FUNC-CONFIG-019

A production operation SHALL require the selected remote to provide
`openocd_command`, either directly or through its selected preset.

### REQ-FUNC-CONFIG-020

After required remote-home resolution of its executable, the configured
`openocd_command` executable and fixed arguments SHALL retain their order as an
opaque argv prefix before runner-generated OpenOCD arguments. Fixed arguments
SHALL otherwise be preserved literally. The runner SHALL NOT parse, classify,
reorder, or validate the fixed arguments' OpenOCD or Tcl semantics.

### REQ-FUNC-CONFIG-021

Runner-owned startup-ordering guarantees SHALL cover only generated arguments,
not arbitrary behavior introduced by fixed arguments.

Note:

Users are responsible for conflicts between fixed `openocd_command` arguments
and with runner-generated arguments, and for avoiding initialization or
listener creation before runner-generated bind or service configuration takes
effect unless they intentionally accept that behavior.

### REQ-FUNC-CONFIG-022

With `west -v`, the runner SHALL log the full effective remote OpenOCD argv,
shell-escaped or otherwise unambiguously separated, including fixed arguments
and generated arguments. Session-specific workspace and address values SHALL
be resolved in generated arguments. Fixed prefix elements SHALL retain literal
placeholder text. This complete argv is diagnostic content and SHALL NOT be
treated as a secret-safe or redacted log; literal sensitive values supplied in
configured command arguments MAY appear in it.

### REQ-FUNC-CONFIG-023

The helper SHALL report the exact effective argv before each spawn attempt, and
the client SHALL deliver it for logging while awaiting readiness. Spawn,
readiness, or required-forwarding failure SHALL NOT suppress this diagnostic.
Bind-collision retries SHALL each report their own effective argv.

### REQ-FUNC-CONFIG-024

Structural validation SHALL apply to every definition at load time. Missing
selected remotes, selected presets, and operational requirements SHALL be
reported only when that remote is used.

### REQ-FUNC-CONFIG-025

Local mapping paths SHALL be normalized before duplicate or conflict detection
and precedence selection. During a real operation, a home-relative
`openocd_command` executable and home-relative remote path-mapping destinations
SHALL be resolved using the remote SSH user's home.

### REQ-FUNC-CONFIG-026

The schema SHALL enforce command lexical validity. Every command element SHALL
exclude NUL. A command executable SHALL be a bare name, an absolute path, or a
current-user `~` path.

### REQ-FUNC-CONFIG-027

The schema SHALL enforce mapping path lexical validity. Mapping keys SHALL be
absolute or current-user `~` local paths and SHALL exclude NUL. Mapping
destinations SHALL be absolute or current-user `~` POSIX paths, SHALL exclude
NUL, and SHALL be lexically normalized: they SHALL NOT contain empty, `.` or
`..` components or a trailing separator, except that `/` and `~` are valid
roots. Local mapping paths MAY contain `.` and `..` because they are resolved
using the local filesystem before collision detection.

---

## 9. Runner Availability and Selection

### REQ-FUNC-SELECT-001

For a build which registers `openocd`, module integration SHALL also register:

```text
remote_openocd
```

### REQ-FUNC-SELECT-002

Adding `remote_openocd` SHALL NOT remove the built-in `openocd` runner.

### REQ-FUNC-SELECT-003

The developer SHALL be able to explicitly select local OpenOCD using:

```text
-r openocd
```

### REQ-FUNC-SELECT-004

The developer SHALL be able to explicitly select remote OpenOCD using:

```text
-r remote_openocd
```

### REQ-FUNC-SELECT-005

Per-user configuration SHALL allow the developer to choose whether `openocd` or `remote_openocd` is the default runner for OpenOCD-capable builds.

### REQ-FUNC-SELECT-006

Changing the default-runner preference SHALL NOT require source-repository modification.

### REQ-FUNC-SELECT-007

Explicit `-r` selection SHALL override the generated default.

### REQ-FUNC-SELECT-008

Changes to user configuration that affect generated Zephyr runner state SHALL
be detected by the normal Zephyr build and reconfiguration machinery for builds
using the module. After such a change, a normal build or reconfiguration
operation SHALL regenerate the affected runner state before a subsequent west
runner command consumes it. This SHALL be possible without a pristine build or
a full firmware compile and link. A runner command is not required to perform
a full firmware build solely to apply that configuration change.

### REQ-FUNC-SELECT-010

If rebuilding or CMake regeneration is explicitly disabled, such as by a
`--no-rebuild` runner option, the implementation SHALL NOT be required to
update existing generated state until a later regeneration.

---

## 10. Board Configuration Reuse

### REQ-FUNC-BOARD-001

The custom runner SHALL reuse the following common `RunnerConfig` values when
Zephyr provides them:

- board directory;
- ELF path;
- BIN path;
- HEX path;
- GDB path;
- OpenOCD search paths.

### REQ-FUNC-BOARD-002

For builds that register the built-in `openocd` runner, board-specific runner
arguments generated by Zephyr for `openocd` SHALL also be made available to
`remote_openocd`.

Representative argument types include:

```text
--cmd-load
--cmd-verify
--file-type
```

and their associated values.

### REQ-FUNC-BOARD-003

Users SHALL NOT need to duplicate built-in OpenOCD board-runner arguments in:

- application source;
- board source;
- user-specific patches.

---

## 11. OpenOCD Runner Compatibility

### REQ-FUNC-OPT-001

For the west commands in REQ-FUNC-SCOPE-002, the custom runner SHALL accept
the user-facing runner option names and value forms that Zephyr 4.4 exposes for
the built-in `openocd` runner. Unless another requirement in this SRS defines
different remote-execution behavior, inherited options SHALL preserve their
functional effect on runner configuration and generated OpenOCD or GDB
behavior. Identical diagnostics, logging, help text, or other incidental
presentation SHALL NOT be required.

The custom runner MAY add remote-specific options.

Rationale:

This defines a versioned functional compatibility boundary without duplicating
Zephyr's runner option list or incidental presentation behavior in this
specification.

### REQ-FUNC-OPT-002

The runner SHALL support probe selection through Zephyr 4.4's `--serial` option.

Rationale:

Remote hosts may contain multiple otherwise equivalent probes.

### REQ-FUNC-OPT-003

If `--serial` is omitted, the custom runner SHALL NOT invent a serial-selection requirement.

### REQ-FUNC-OPT-004

The runner SHALL support distinct:

- remote OpenOCD GDB-server port;
- local GDB-client port;

where Zephyr exposes both.

### REQ-FUNC-OPT-006

User-supplied OpenOCD command options inherited under REQ-FUNC-OPT-001 SHALL
retain their Zephyr 4.4 semantics when constructing the remote OpenOCD command.

### REQ-FUNC-OPT-007

The runner SHALL NOT be required to translate arbitrary local paths embedded in arbitrary user-written Tcl.

---

## 12. Runtime Environment Forwarding

### REQ-FUNC-ENV-001

The runner SHALL support an explicit allow-list of local environment-variable names forwarded to remote OpenOCD.

### REQ-FUNC-ENV-002

The complete local environment SHALL NOT be forwarded implicitly.

### REQ-FUNC-ENV-003

Forwarded variables SHALL be available to remote OpenOCD before it processes configuration files.

Rationale:

Runtime values may influence probe, debug adapter, or target configuration while
OpenOCD configuration files are being evaluated.

### REQ-FUNC-ENV-004

If an allow-listed variable is absent locally, the runner SHALL:

1. emit a non-fatal warning;
2. not forward a local value for that variable to remote OpenOCD;
3. continue execution.

This requirement does not request removal of a same-named variable from the
helper's inherited environment; the remote value, if present, may remain
available to OpenOCD.

---

## 13. Remote Host and OpenOCD

### REQ-FUNC-REMOTE-001

OpenOCD SHALL execute on a configured remote Linux host.

### REQ-FUNC-REMOTE-002

The remote OpenOCD executable SHALL be configurable per user.

### REQ-FUNC-REMOTE-003

The remote OpenOCD executable SHALL NOT be required to exist in the remote user's `PATH`.

### REQ-FUNC-REMOTE-004

The local development host SHALL retain:

- GDB;
- source files;
- debug symbols;
- local build output used by development tools;
- the compiler/toolchain.

---

## 14. OpenOCD Configuration and Files

For REQ-FUNC-FILE-001 through REQ-FUNC-FILE-007 and REQ-FUNC-FLASH-002 through
REQ-FUNC-FLASH-003, a required file or required local search directory is one
identified through a supported runner input, such as a `RunnerConfig` path, a
Zephyr-generated OpenOCD runner argument or search path, a supported runner
option, or an explicit configured path mapping. Files or directories referenced
only by arbitrary user-written Tcl or opaque fixed `openocd_command` arguments
are outside this definition.

### REQ-FUNC-FILE-001

Files directly required by remote OpenOCD SHALL be accessible on the remote host.

### REQ-FUNC-FILE-002

Board-specific OpenOCD configuration from the developer's local Zephyr tree
identified through supported runner inputs SHALL remain usable remotely within
that input boundary.

### REQ-FUNC-FILE-003

Board OpenOCD configuration SHALL retain the ability to source common
configuration files identified through supported runner inputs when the
equivalent local OpenOCD setup can resolve them. This compatibility requirement
does not require translation of arbitrary local paths embedded in arbitrary
user-written Tcl or alteration of fixed arguments treated as opaque under
REQ-FUNC-CONFIG-020.

### REQ-FUNC-FILE-004

The runner SHALL support explicit recursive local-to-remote path mappings.
Mapping matches SHALL be component-aware. Overlapping mappings with distinct
normalized local roots are valid; when a required local path matches more than
one mapping, the mapping with the most specific (longest) normalized local root
SHALL take precedence. For example, with `/src` mapped to `/remote/a` and
`/src/board` mapped to `/remote/b`, paths under `/src/board` SHALL use
`/remote/b` and other paths under `/src` SHALL use `/remote/a`. The selected
mapping SHALL translate the path by appending its path relative to the selected
local root to the selected remote root. An overlapping ancestor mapping SHALL
NOT also apply to that path or create a mapping collision.

### REQ-FUNC-FILE-005

A required local file not covered by an explicit mapping SHALL be staged into the current remote session.

### REQ-FUNC-FILE-006

A required local search directory not covered by an explicit mapping SHALL be
staged while preserving relative structure required by OpenOCD lookup,
including an empty search root and empty nested directories.

### REQ-FUNC-FILE-007

The runner SHALL preserve every OpenOCD search path supplied by the Zephyr
build, even when the current board configuration appears not to use it.

Relative OpenOCD configuration file references SHALL resolve against the local
current working directory first, then the supplied search directories in their
original order. The selected file SHALL use the existing path mapping or staging
behavior, including reuse of a staged search tree.

### REQ-FUNC-FILE-008

The runner SHALL NOT maintain a persistent cross-session firmware or configuration cache.

---

## 15. Flash

### REQ-FUNC-FLASH-001

Unless verification-only behavior is requested, `west flash -r remote_openocd`
SHALL program the intended remote target and start the selected image.

### REQ-FUNC-FLASH-002

Firmware identified through supported runner inputs and directly required by
remote OpenOCD SHALL be staged when not available through a configured mapping.

### REQ-FUNC-FLASH-003

Runner-generated OpenOCD commands and Tcl that reference remotely accessed
firmware identified through supported runner inputs SHALL reference its mapped
or staged remote path and preserve literal path characters. The runner SHALL
NOT rewrite user-provided Tcl or opaque fixed arguments.

### REQ-FUNC-FLASH-004

Flash-related options inherited under REQ-FUNC-OPT-001 SHALL retain their
Zephyr 4.4 `openocd` runner semantics for:

- erase;
- load;
- verification;
- verification-only;
- firmware file selection;
- custom OpenOCD commands.

Remote firmware staging and path rewriting SHALL follow REQ-FUNC-FLASH-002 and
REQ-FUNC-FLASH-003.

### REQ-FUNC-FLASH-005

A failed remote OpenOCD flash operation SHALL cause the west operation to fail.

---

## 16. Debug and Attach

### REQ-FUNC-DEBUG-001

GDB SHALL execute locally.

### REQ-FUNC-DEBUG-002

The OpenOCD GDB server SHALL execute remotely.

### REQ-FUNC-DEBUG-003

The custom runner SHALL establish required local-to-remote GDB transport before launching local GDB.

### REQ-FUNC-DEBUG-005

`west attach -r remote_openocd` SHALL connect local GDB without flashing. The
session SHALL allow GDB to read the program counter and the instruction at that
address.

### REQ-FUNC-DEBUG-006

`west debugserver -r remote_openocd` SHALL expose a locally reachable GDB-server
endpoint backed by remote OpenOCD without launching local GDB. The endpoint SHALL
allow an independent GDB client to control the target.

---

## 17. OpenOCD Network Services

### REQ-FUNC-SVC-001

The custom runner SHALL select local forwarding from the requested operation
and runner options rather than by discovering the effective remote OpenOCD
configuration.

The service and forwarding configuration SHALL be:

- no local forwards for `flash`;
- GDB for `debug`, `attach`, and `debugserver`;
- Tcl and telnet for `debug`, `attach`, and `debugserver` unless the
  corresponding runner port option is `disabled`;
- GDB plus enabled Tcl/telnet for initial `rtt` setup; after batch GDB setup,
  RTT forwarding is required and GDB forwarding becomes best-effort;
- RTT when the selected operation requests an RTT endpoint.

The selected service set and forwarding requirement SHALL be distinct:

| Operation | Required initially | Required during the client operation | Best-effort forwarding |
| --- | --- | --- | --- |
| `debug` | GDB; RTT when `--rtt-server` is requested | GDB; requested RTT | Tcl, telnet |
| `attach` | GDB | GDB | Tcl, telnet |
| `debugserver` | GDB; RTT when `--rtt-server` is requested | GDB; requested RTT | Tcl, telnet |
| `rtt` | GDB for batch setup | RTT after batch setup | Tcl, telnet; GDB after setup |
| `flash` | None | None | None |

Required forwarding startup or runtime failure SHALL fail the operation.
Best-effort forwards SHALL be attempted independently, so failure of one
cannot roll back another active forward. Best-effort startup failure SHOULD
warn and allow the required operation to continue only when startup rollback
succeeds. Best-effort runtime failure SHOULD warn at the next forwarding
status check and SHALL NOT terminate an otherwise usable required operation.
Concurrent supervision or interruption of interactive GDB is not required.

RTT forwarding for the `rtt` command SHALL remain deferred until successful
batch GDB setup. After that setup succeeds, GDB forwarding SHALL become
best-effort before RTT forwarding is established as required. Forwarding
failures SHALL be classified as required or best-effort when the runner checks
them.

Explicitly requested RTT forwarding for `debug --rtt-server` and
`debugserver --rtt-server` SHALL be required at startup and throughout the
operation. This requirement concerns SSH forwarding only; the runner SHALL
NOT probe the remote RTT service. A local forward does not guarantee that a
corresponding remote listener is available.

### REQ-FUNC-SVC-002

A disabled OpenOCD service SHALL NOT require a corresponding local listener.

### REQ-FUNC-SVC-003

Local forwarded services SHALL bind only to local loopback interfaces.

### REQ-FUNC-SVC-004

Runner-generated configuration SHALL bind remote OpenOCD services only
to the runner-allocated remote loopback address. These startup-ordering
guarantees do not cover arbitrary behavior from advanced fixed
`openocd_command` arguments (REQ-FUNC-CONFIG-020 and REQ-FUNC-CONFIG-021). The
remote bind address and service-port settings are runner-owned transport
properties. Board or user Tcl
that overrides `bindto`, `gdb_port`, `tcl_port`, `telnet_port`, or another
runner-owned service port is outside the supported compatibility boundary.
The runner SHALL NOT be required to statically inspect arbitrary Tcl for such
overrides.

### REQ-FUNC-SVC-005

If a required local service port is occupied, the operation SHALL fail rather
than silently choose another port. An occupied best-effort local port SHOULD
produce a warning and allow the required operation to continue, provided
rollback of the failed forwarding attempt succeeds.

### REQ-FUNC-SVC-006

A local-port conflict error or warning SHALL identify the affected service
and port.

---

## 18. RTT

### REQ-FUNC-RTT-001

The runner SHALL support RTT channel 0.

### REQ-FUNC-RTT-002

RTT SHALL support bidirectional communication.

### REQ-FUNC-RTT-003

`west rtt -r remote_openocd` SHALL configure RTT using remote OpenOCD and launch the local RTT client.

### REQ-FUNC-RTT-004

Custom `--rtt-port` values SHALL be supported.

### REQ-FUNC-RTT-005

`west debug -r remote_openocd --rtt-server` SHALL provide GDB and a bidirectional
RTT service during the same runner invocation. Both SSH forwards SHALL be
required; RTT forwarding startup or observed runtime failure SHALL fail the
operation without probing the RTT service.

### REQ-FUNC-RTT-006

Where the runner supports RTT, `west debugserver` with `-r remote_openocd` and
`--rtt-server` SHALL expose endpoints for an independent GDB client and a
bidirectional RTT connection. Both SSH forwards SHALL be required; RTT
forwarding startup or observed runtime failure SHALL fail the operation
without probing the RTT service.

### REQ-FUNC-RTT-007

The runner SHALL NOT require GDB Remote Serial Protocol inspection solely to determine RTT configuration.

---

## 19. Semihosting Console

Semihosting console output is the semihosting behavior explicitly guaranteed
by this runner. Other semihosting behavior can be available through OpenOCD or
GDB without runner involvement. Semihosting operations handled directly by
OpenOCD execute according to OpenOCD behavior on the remote host. Such behavior
is outside the runner's compatibility guarantees.

### REQ-FUNC-SEMI-001

Ordinary OpenOCD commands used to enable semihosting SHALL be accepted through
inherited command options such as `--cmd-pre-init`.

### REQ-FUNC-SEMI-002

Semihosting console output emitted by remote OpenOCD on stdout/stderr SHALL appear in the local west terminal.

### REQ-FUNC-SEMI-003

The runner SHALL NOT require a dedicated semihosting network protocol or proxy.

### REQ-FUNC-SEMI-004

The runner SHALL NOT implement, configure, proxy, virtualize, or path-translate
GDB File-I/O for semihosting. GDB File-I/O provided transparently by remote
OpenOCD and a locally connected GDB MAY work without runner involvement and MAY
access the filesystem of the host running GDB. Such transparent behavior is
outside the runner's compatibility guarantees.

---

## 20. SSH Client Selection and Compatibility

### REQ-FUNC-SSH-001

The runner SHALL use a configured OpenSSH-compatible external client command
for every SSH transport operation.

### REQ-FUNC-SSH-002

The default SSH command SHALL use `ssh` resolved from the local host's normal command search path.

### REQ-FUNC-SSH-003

The per-user configuration SHALL allow the complete SSH command prefix to be
overridden as an argv sequence.

### REQ-FUNC-SSH-004

The first SSH argv element SHALL permit either a bare executable resolved
through `PATH` or an explicit executable path. The implementation SHALL NOT
assume that its basename is `ssh`.

### REQ-FUNC-SSH-005

Fixed user-supplied SSH argv elements SHALL be preserved exactly and in order.
For runner-owned local forwards, `ExitOnForwardFailure=yes` and
`ClearAllForwardings=no` SHALL take precedence over conflicting fixed arguments
and normal SSH configuration, so generated local forwarding cannot be silently
discarded and local bind failure remains fatal.

### REQ-FUNC-SSH-006

The runner SHALL invoke the SSH argv directly without inserting a shell.

### REQ-FUNC-SSH-007

The configured SSH command SHALL be used consistently for remote-runner SSH
operations.

### REQ-FUNC-SSH-009

The runner SHALL NOT require users to duplicate normal SSH credentials, keys, or proxy configuration in the remote-runner configuration.

### REQ-FUNC-SSH-010

When the configured SSH client reports loss of the controlling SSH session,
the runner SHALL fail the local operation and make its bounded local session-
cleanup attempt. This local observation SHALL NOT be treated as evidence that
the remote helper has observed control-channel loss or begun remote OpenOCD
cleanup.

Local SSH-loss detection latency SHALL be delegated to the configured SSH
client and the local operating system. This requirement does not impose an
end-to-end bound from the underlying connection loss to local detection.

### REQ-FUNC-SSH-011

The runner SHALL NOT attempt transparent reconstruction of an interrupted debugging session after SSH loss.

---

## 21. Concurrent Users and Probe Contention

### REQ-FUNC-CONC-001

Multiple developers SHALL be able to operate independent remote sessions concurrently.

### REQ-FUNC-CONC-002

The runner SHALL NOT prevent concurrent sessions solely because their
independently usable channels belong to the same physical probe.

### REQ-FUNC-CONC-003

The project SHALL NOT implement an additional board reservation service.

### REQ-FUNC-CONC-004

If OpenOCD cannot acquire the requested probe or channel because another process owns it, the later operation SHALL fail rather than be queued.

---

## 22. Remote Helper

### REQ-FUNC-HELP-001

Routine helper installation and execution SHALL NOT require root privileges.

### REQ-FUNC-HELP-002

The current remote helper SHALL be automatically deployable to the remote user's
account as part of the client/helper deployment.

### REQ-FUNC-HELP-003

The project SHALL NOT require a persistent privileged or system-wide daemon.

### REQ-FUNC-HELP-004

The helper SHALL supervise remote OpenOCD. Helper failure, orderly session
closure, and natural OpenOCD termination SHALL remain distinct outcomes, and
helper, protocol, transport, and cleanup failures SHALL NOT be represented as
OpenOCD exit statuses. The helper SHALL remain self-contained and require only
the Python 3.12+ standard library on the remote host.

### REQ-FUNC-HELP-005

After the remote helper observes that its controlling SSH channel has ended,
whether through EOF or a termination signal, it SHALL initiate the bounded
process-group cleanup specified in REQ-FUNC-HELP-012. When process-group
signalling and reaping complete successfully within that cleanup attempt, the
associated OpenOCD process SHALL be terminated. If termination fails or cannot
be confirmed within the bound, the helper SHALL record a cleanup failure and
report the unsuccessful termination as a helper/session failure when control
output remains usable. Loss of the control channel MAY prevent delivery of
that report. Remote SSH/operating-system detection latency is outside this
requirement's bound. Local SSH-client detection SHALL NOT be treated as remote
helper observation, and the project SHALL NOT bound the interval from local
detection to remote OpenOCD termination.
The helper SHALL continue observing control input while process readiness is
pending, without waiting for readiness success, failure, or timeout.

### REQ-FUNC-HELP-006

The client and helper SHALL validate the session control contract before
acting on commands or events. The helper SHALL report readiness only after the
configured process-readiness conditions are met. A process with no configured
readiness conditions SHALL be ready immediately. The helper SHALL preserve
child-output ordering within each stream. Orderly session closure and helper
failure SHALL remain distinguishable, and either SHALL end the session. Loss
of the control transport MAY prevent delivery of a final outcome.

The exact Protocol v1 messages, fields, framing, state transitions, ordering,
validation rules, and frame-size limits SHALL be defined solely in
[`protocol.md`](../architecture/protocol.md). Automatic helper deployment
SHALL provide the helper revision matching the local client.

### REQ-FUNC-HELP-007

Concurrent helper deployments SHALL be safe: they SHALL NOT expose a partial
helper revision or remove the revision selected by an active deployment. Stale
helper revisions matching the deployment naming scheme and older than 24 hours
SHALL be eligible for opportunistic reclamation during automatic deployment,
excluding the selected revision. Deployment SHALL attempt to reclaim each
eligible stale revision; failure to remove one SHALL NOT fail deployment or
affect the selected revision.

### REQ-FUNC-HELP-008

Service configuration SHALL be validated before process startup. The client
and helper SHALL independently validate the portions of the service contract
available at their respective boundaries. The exact Protocol v1 request
fields and validation rules are defined in
[`protocol.md`](../architecture/protocol.md).

This validation applies to the runner-selected service and forwarding
configuration; it does not
discover or validate the effective service state produced by arbitrary OpenOCD
Tcl.

### REQ-FUNC-HELP-009

When the helper reports natural OpenOCD termination through the valid session
helper protocol, the client SHALL preserve the reported integer
exit status as the OpenOCD result. Helper-process status, SSH/control-
transport status, forwarding-process status, protocol failures, and cleanup
failures SHALL NOT be represented as OpenOCD exit statuses. Client-requested
termination that completes without a natural OpenOCD termination result SHALL
NOT synthesize an OpenOCD exit status.

### REQ-FUNC-HELP-010

Closing a remote session SHALL make one bounded attempt to clean up all locally and
remotely owned session resources. For ordinary coordinated shutdown, all
applicable cleanup actions SHALL be attempted even when an earlier cleanup
action fails. When SSH loss prevents coordinated shutdown, local and remote
cleanup are independent: each side SHALL make one bounded attempt to clean up
the resources it owns after that side observes the loss. These SSH-loss
cleanup bounds do not include loss-detection latency or the interval before
the other side observes the loss. Repeated shutdown requests SHALL be
harmless. Successful continuation or retry of a partially failed cleanup
sequence SHALL NOT be required. Failure to terminate an owned process or to
release an owned resource within the cleanup attempt is a cleanup failure and
SHALL remain visible under REQ-FUNC-HELP-011. The remote helper SHALL report
such an unsuccessful termination as a helper/session failure instead of a
successful session close when its control output remains usable.

### REQ-FUNC-HELP-011

When an operation failure has already been established, later cleanup
failures, session/infrastructure failures, or OpenOCD-result observations
SHALL NOT replace that failure. Later failures and relevant OpenOCD results
SHOULD remain available as diagnostic information. When no earlier failure
exists, helper, protocol, SSH/control, required-service forwarding, or
required-shutdown failure SHALL fail the operation. Best-effort forwarding
startup or runtime failure SHOULD produce a warning, provided
failed startup rollback succeeds. Cleanup failures affecting acquired resources
SHALL remain operation-fatal regardless of whether the service was required
or best-effort; this includes failed rollback of a best-effort startup attempt
and failed later cleanup of an owned best-effort process. Such failures SHALL
remain distinct from OpenOCD exit status.
Session-fatal helper, protocol, or control observations recorded while an
active local client is running SHALL be acted upon at the next session status
check after that client returns. The runner is not required to asynchronously
interrupt the local client solely because such an observation was recorded. A
failure that itself removes required transport MAY naturally cause the local
client to return earlier.

### REQ-FUNC-HELP-012

REQ-FUNC-HELP-012 applies to OpenOCD launched as the session process of a
remote-runner session. Standalone helper operations, including OpenOCD version
probing, are not session processes and are outside the scope of the session
helper's process-group supervision contract.

The remote session's OpenOCD process SHALL execute within a helper-owned
process-group boundary. Once the helper observes loss or termination of the
controlling session, it SHALL begin bounded cleanup of that owned process
group and associated session resources. This cleanup bound does not include
the time required for the remote SSH service or operating system to deliver
that observation, nor the interval after an independent local observation and
before the remote helper observes the loss. Cleanup SHALL attempt to terminate
the complete owned process group and release the OpenOCD leader and owned
relay resources. Diagnosis of surviving descendants when observable SHOULD be
provided, but failure of best-effort descendant inspection SHALL NOT by itself
make otherwise successful process-group cleanup fail. The exact signal, wait,
inspection, escalation, reaping, and relay-cleanup algorithm belongs in the
SAD. Successful termination is required when the termination and reaping
operations complete successfully within the bounded cleanup attempt. Otherwise,
the attempt is unsuccessful and conformance requires the cleanup-failure
recording and reporting specified by REQ-FUNC-HELP-010 and REQ-FUNC-HELP-011;
a failed attempt is not required to guarantee that the process group has
terminated.

---

## 23. Session Data

### REQ-FUNC-DATA-002

Remote session files SHALL be protected from other ordinary remote users by filesystem permissions.

### REQ-FUNC-DATA-003

Normal session termination SHALL remove temporary session artifacts.

Cleanup SHALL NOT remove a workspace while staging operations already using it
are validating or extracting their archives or reporting success. Once cleanup
begins, new staging operations SHALL NOT use or recreate that workspace, even
if cleanup fails or the workspace has been deleted. A stalled staging operation
SHALL NOT prevent a bounded cleanup attempt or prevent cleanup of other
sessions. Cleanup failures SHALL remain visible and SHALL NOT permit new
staging operations to resume use of the workspace.

### REQ-FUNC-DATA-005

A session workspace older than 24 hours SHALL be eligible for reclamation when
the helper can establish that it is not owned by an active session and is not
protected by an active staging operation.

When allocating a new session, the helper SHALL attempt to reclaim each
workspace that it can safely establish as eligible. If eligibility cannot be
safely established, or if inspection or removal fails, the helper MAY leave the
workspace for a later allocation. Such failure SHALL NOT require another
reclamation attempt during the same allocation.

Normal cleanup failures SHALL remain visible under REQ-FUNC-DATA-003.
Opportunistic reclamation does not guarantee bounded disk growth when no later
allocation occurs or when inspection or removal continues to fail.

---

## 24. Platform Requirements

### REQ-NFUNC-PLAT-001

The local platform SHALL be Linux with Python 3.12 or newer.

### REQ-NFUNC-PLAT-002

The remote platform SHALL be Linux with Python 3.12 or newer.

---

## 25. Other Non-Functional Requirements

### REQ-NFUNC-ADMIN-001

Routine use, helper deployment, upgrades, cleanup, and diagnostics SHALL NOT require root privileges.

### REQ-NFUNC-TEST-001

Runner option and OpenOCD command construction SHALL be testable without physical hardware.

### REQ-NFUNC-TEST-002

Remote-session and forwarding behavior SHALL be testable using fake OpenOCD endpoints.

---

## 26. Assumptions

### ASM-001

Developers have ordinary SSH access to the remote host.

### ASM-002

Developers use separate remote Unix accounts.

### ASM-003

Lab users are trusted.

### ASM-004

OpenOCD/debug-probe acquisition provides acceptable exclusion when a probe/channel is occupied.

### ASM-005

Independent probe channels supported by the underlying hardware and OpenOCD may be controlled by independent OpenOCD processes.

### ASM-006

The remote Linux host can run multiple OpenOCD instances using identical TCP port numbers when bound to different loopback addresses.


### ASM-007

Firmware and configuration artifacts are sufficiently small that a persistent artifact cache is unnecessary.

---

## 27. Explicit Non-Goals

The current scope does not include:

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
- runner-provided configuration, proxying, virtualization, or path translation
  for semihosting GDB File-I/O;
- requiring a specific SSH-agent implementation.

---

## 28. Major Risks

### RISK-003 - Zephyr-version API coupling

Zephyr supports `runners.core` as its external-runner API but makes no such
guarantee for `OpenOcdBinaryRunner`. Reusing that class therefore requires
version-specific maintenance.

Severity: Medium.

Mitigation:

Keep all use of `OpenOcdBinaryRunner` in the Zephyr 4.4 compatibility layer.
Do not use private attributes or methods. Validate or update the runner
integration for each newly supported Zephyr version.

### RISK-007 - SSH client differences

OpenSSH-compatible clients can differ in supported options, process behavior,
authentication, and forwarding behavior.

Severity: Medium.

Mitigation:

Depend only on the required OpenSSH-compatible behavior and preserve configured
argv.
