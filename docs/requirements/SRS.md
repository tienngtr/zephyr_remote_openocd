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

The external SSH client command selected by the user for remote communication.
For this specification, a compatible SSH client supports:

- executing a remote command while carrying stdin to that command, relaying
  stdout and stderr, and returning its exit status;
- directional EOF for the long-lived helper: closing local stdin causes EOF on
  remote command stdin while leaving stdout and stderr usable until the remote
  command exits;
- local TCP forwarding with an OpenSSH-compatible `-L` option;
- the runner-owned forwarding controls `ExitOnForwardFailure=yes` and
  `ClearAllForwardings=no` with their stated OpenSSH-compatible behavior.

Wrappers that suppress stdin, close both directions on input closure, require
PTY behavior that prevents directional EOF, or detach/reparent the helper so
controller lifetime no longer owns the session are incompatible. A fallback
shutdown command is not required for such wrappers.

This capability definition does not require compatibility with the complete
OpenSSH feature set.

This will normally be `ssh` found through `PATH`, but configuration may name
another executable and fixed arguments.

### 2.9 Remote helper

An unprivileged per-user program executed on the remote host to:

- create and clean remote-session state;
- stage files;
- allocate a session address;
- launch and supervise OpenOCD;
- relay OpenOCD output.

### 2.10 Remote session

The runner-managed operation containing a remote helper, workspace, OpenOCD
process and allocated address, and local SSH transports, forwarded services,
and any dependent local client. Local operation, remote helper, forwarding,
and RTT have separate ownership and failure domains; this term does not imply
one global lifecycle authority.

Each side owns the resources it acquires. SSH connection-sharing state is
externally managed. The runner owns the SSH subprocesses it launches, but not
a sharing master or forwarding state retained by an external multiplexing
mechanism after those subprocesses exit.

### 2.11 Helper control channel

The connection used to start a remote session, retain controller-input lifetime,
and receive attempt diagnostics, OpenOCD output, startup readiness, and the
terminal outcome. After startup is requested, input remains open while the
local operation owns the session. Intentional release marks the end of that
lease and then ends input; input EOF without that release marker is lease
loss. Output may remain usable for final diagnostics and cleanup confirmation.
A continuing command stream is not required. The wire grammar belongs in the
protocol document.

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

### 3.5 Bounded cleanup attempt

A bounded cleanup attempt SHALL NOT wait or retry indefinitely in project-
controlled cleanup logic. Each project-controlled wait or retry SHALL have a
finite deadline. Expiration SHALL be reported as cleanup failure and SHALL NOT
prevent attempts at other applicable independent cleanup actions. The bound
begins when that side starts cleanup. Observation or detection latency, and
ordinary blocking behavior of individual operating-system or filesystem
operations that the project does not wrap with a deadline, are outside this
guarantee. Timeout, cancellation, and an unsuccessful wait SHALL NOT establish
that an acquisition producer has finished, that a process has been disposed of,
or that remote cleanup succeeded. Unconfirmed disposal SHALL remain explicit.

Note:

A bounded cleanup attempt is a bounded-effort guarantee for project-controlled
cleanup logic, not an end-to-end wall-clock guarantee that cleanup will always
complete within a fixed elapsed time. In particular, this specification does
not bound failure-detection latency or blocking performed inside operating-system,
filesystem, subprocess, or other external operations for which the project has
not established its own deadline.

This distinction is intentional. A requirement for a fixed total cleanup
duration would require stronger platform and external-operation assumptions
than this specification makes. The absence of such a total duration therefore
does not by itself constitute an unspecified cleanup bound.

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
8. Reuse the built-in `openocd` runner from the particular supported Zephyr
   4.4.x environment in use while defining remote-specific behavior explicitly
   in this specification.
9. Keep Zephyr-version-specific integration isolated from the generic remote subsystem.
10. Support Linux as the local platform.
11. Avoid imposing a particular SSH-key or SSH-agent arrangement on developers.

---

## 5. Scope

### REQ-FUNC-SCOPE-001

The runner SHALL target the Zephyr 4.4 release series (4.4.x).

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
prohibit reuse of dependencies supplied by the supported Zephyr 4.4.x runner
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

The setup program SHALL create the default configuration directory with mode
`0700` and the configuration file with mode `0600`, without changing
permissions on any pre-existing parent, directory, or file. For a configuration
path other than the default, the setup program SHALL require the path's parent
directory to exist and SHALL create only the file, with mode `0600`.

### REQ-FUNC-INSTALL-011

The setup program SHALL report whether `pyelftools`, PyYAML, and jsonschema are
discoverable in the active Python environment. A missing dependency SHALL
produce a warning directing the user to the Python environment configured for
the supported Zephyr 4.4.x environment, but SHALL NOT prevent configuration
initialization or recommend a separate product installation.

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
YAML input SHALL be parsed as data only: parsing SHALL NOT construct arbitrary
language objects or execute configuration-supplied code. Duplicate YAML keys
SHALL be rejected before schema validation.

### REQ-FUNC-CONFIG-003

The setup operation SHALL create the configuration file if it does not already
exist, at the path given as its optional argument or otherwise at the default
path of REQ-FUNC-CONFIG-001. It SHALL NOT consult
`ZEPHYR_REMOTE_OPENOCD_CONFIG`.

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
or exposing an unhandled parser traceback. Schema validation SHALL use the
selection-level contract for CMake default selection (REQ-FUNC-SELECT-011)
and the complete contract for runtime configuration loading.

### REQ-FUNC-CONFIG-009

Module upgrades SHALL NOT automatically rewrite an existing user configuration merely to add optional settings or comments.

### REQ-FUNC-CONFIG-011

During full configuration loading, unknown keys, explicit nulls, duplicate
YAML keys, invalid types, disallowed empty command executables, SSH hosts
containing NUL, duplicate environment names, invalid paths, duplicate mappings,
and conflicting mappings SHALL produce actionable configuration errors.

For path mappings, a duplicate mapping has a local key that reaches the same
existing filesystem object as an already defined key with the same destination.
A conflicting mapping reaches the same existing object with a different
destination. Distinct local roots in an ancestor/descendant relationship are
overlapping mappings and SHALL NOT be treated as conflicts. Keys whose roots do
not exist cannot be duplicates or conflicts.

### REQ-FUNC-CONFIG-012

Full runtime configuration loading SHALL accept or reject documents according
to the canonical machine-readable schema for every structural and lexical rule
that the schema expresses. CMake selection validation SHALL use a reusable
fragment of that same schema, also incorporated by the full configuration schema;
selection rules SHALL NOT be duplicated in handwritten validators. Both layers
SHALL share the YAML parsing implementation and its safeguards. The
implementation SHALL additionally enforce the contextual semantics required
by this specification where the schema cannot
express them, including local path resolution and collision detection,
selected-definition references, operationally required settings, and
remote-home expansion.

### REQ-FUNC-CONFIG-013

The module SHALL provide a user-facing command that validates an existing
configuration and summarizes its effective settings without SSH,
OpenOCD, GDB, subprocess, socket, or hardware operations.

For this summary, the omitted data is the current local values of environment
variables named by `forward_env`. The command SHALL NOT retrieve those values
solely because their names appear in `forward_env`, and SHALL NOT display those
values as forwarded-environment settings, but MAY print their names. This does
not prohibit reading an environment variable when independently required for
configuration discovery, path resolution, or other behavior defined by this
specification. Results of such independent behavior, such as a resolved
configuration path, MAY appear in the summary. The summary SHALL include
configured SSH and OpenOCD argv elements, hosts, path mappings, and selected
definition names as diagnostic content. It SHALL NOT infer or redact secrets
from command arguments or paths; configured command arguments MAY therefore
contain values that are sensitive to the user.

The command SHALL resolve an explicitly named remote, otherwise
`default_remote`, while deliberately ignoring
`ZEPHYR_REMOTE_OPENOCD_REMOTE`. When neither is selected, it SHALL report
structural success and the available definitions. The command SHALL validate
the file named by its optional path argument, otherwise the default path of
REQ-FUNC-CONFIG-001, and SHALL NOT consult `ZEPHYR_REMOTE_OPENOCD_CONFIG`. A
missing target file SHALL be an actionable validation failure rather than
being treated as empty runtime configuration.

### REQ-FUNC-CONFIG-014

An unset or empty `ZEPHYR_REMOTE_OPENOCD_CONFIG` environment variable SHALL
select the canonical per-user configuration path. A non-empty value SHALL
override that path for runner commands and build integration and SHALL be an
absolute pathname without `..` path components. Relative values,
including `./...`, `../...`, `~`, `~/...`, and `~user/...`, and absolute
values containing a `..` component SHALL fail with an actionable
configuration error; the application SHALL NOT expand tilde-valued overrides.
`.` components SHALL remain valid. Only complete `..` components are
disallowed; names such as `.config` and `config..yaml` SHALL remain valid.
Tilde characters within an absolute pathname SHALL be treated literally,
including directory names such as `~` or `~user`.

For accepted absolute overrides, the supplied pathname SHALL reach filesystem
reads unchanged, without lexical or physical path normalization. For the same
non-empty override and unchanged filesystem and environment inputs, build-time
selection and runtime `remote_openocd`
SHALL use the same pathname semantics and SHALL NOT silently consume different
configuration files. Build change detection SHALL observe the file selected
by those semantics.

Shell expressions such as `"$PWD/config.yaml"` or
`"$HOME/lab/config.yaml"` can construct absolute values before invoking the
application. The setup operation and configuration validator retain their
explicit-path argument semantics
(REQ-FUNC-CONFIG-003, REQ-FUNC-CONFIG-013).

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

Built-in defaults SHALL be `default_runner: openocd`, `ssh_command: [ssh]`,
empty `forward_env`, and empty `path_mappings`. When `ssh_host` is omitted, it
SHALL default to the selected remote name.

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

The runner SHALL make the exact effective remote OpenOCD argv available as
diagnostic content before the child creation call, including a call that later
fails. Spawn, readiness, or required-forwarding
failure SHALL NOT suppress this diagnostic. Wire-event and framing details are
defined in [protocol.md](../architecture/protocol.md). The helper SHALL accept
the complete diagnostic for delivery before attempting the spawn; inability to
accept it SHALL prevent that attempt. Admission does not guarantee network delivery or peer receipt.

### REQ-FUNC-CONFIG-024

Structural validation SHALL apply to every definition at load time. Missing
selected remotes, selected presets, and operational requirements SHALL be
reported only when that remote is used.

### REQ-FUNC-CONFIG-025

Local mapping roots SHALL be identified by the filesystem object that
traversal of their spelling reaches, not by normalized pathname. They SHALL be
identified when configuration is loaded, for duplicate or conflict detection,
and again when required paths are planned, for duplicate or conflict
detection, matching, and precedence selection. A root that does not exist when
paths are planned, including one that traversal cannot reach, SHALL NOT match
any required path. During a real operation, a home-relative
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
roots. Local mapping paths MAY contain `.`, `..`, and trailing separators
because they are resolved using the local filesystem; they need not exist.

---

## 9. Runner Availability and Selection

### REQ-FUNC-SELECT-001

Module integration SHALL register `remote_openocd` for a build if and only if
that build registers the built-in `openocd` runner.

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

Changes to user configuration at the configuration path selected when the build
was last configured that affect generated Zephyr runner state SHALL be detected
by the normal Zephyr build and reconfiguration machinery for builds using the
module. This includes changes to the contents, creation, or deletion of the
selected configuration file, replacement of that file, and retargeting of a
symbolic link in the selected path. Detection SHALL NOT depend on the selected
file, or a symbolic link target, having a newer modification time than the
generated build files.

After such a change, and before stale generated runner state can be consumed,
a normal build SHALL either regenerate that state within the same invocation or
fail with an actionable diagnostic requiring another build or reconfiguration.
A reconfiguration operation SHALL regenerate that state. Neither outcome SHALL
require a pristine build or a full firmware compile and link. A runner command
is not required to perform a full firmware build solely to apply that
configuration change.

If the current state of the selected configuration file cannot be established,
such as when the file cannot be read, a normal build SHALL NOT proceed with the
previously generated default runner.

Changing `ZEPHYR_REMOTE_OPENOCD_CONFIG` so that it selects a different
configuration path SHALL require CMake reconfiguration before generated runner
state is expected to reflect that path.

### REQ-FUNC-SELECT-010

If rebuilding or CMake regeneration is explicitly disabled, such as by a
`--no-rebuild` runner option, the implementation SHALL NOT be required to
update existing generated state until a later regeneration.

### REQ-FUNC-SELECT-011

For OpenOCD-capable builds with the module active, CMake SHALL validate only
whether the configured default runner can be safely established. Malformed
YAML, duplicate mapping keys, invalid document or root forms, unknown top-level
keys, and an invalid type or value for `default_runner` SHALL fail CMake
configuration with an actionable error. Such failures SHALL NOT implicitly
select local OpenOCD or reuse a previously valid default.

Once shared YAML parsing and selection-level validation succeed, errors that
would be found only by full validation of `default_remote`, `presets`, or
`remotes` SHALL NOT prevent generation of either supported default.
An absent configuration, an empty document, or an omitted `default_runner`
SHALL select the documented `openocd` default. The decision SHALL depend only
on the current configuration and normal current build inputs, never previous
configuration validity, a last-known-good value, or fallback state.

Every actual `remote_openocd` invocation SHALL load and validate the
complete configuration contract before remote effects, regardless of the
configured default or prior CMake selection success. Selection validation
SHALL NOT replace full runtime validation.

### REQ-FUNC-SELECT-012

For OpenOCD-capable builds with the module active, the generated default flash
runner and the generated default debug runner SHALL each be determined
independently, using this precedence from highest to lowest:

1. the developer's build-time Zephyr override for that runner type, given as a
   `BOARD_FLASH_RUNNER` or `BOARD_DEBUG_RUNNER` CMake cache variable, such as
   with `-DBOARD_FLASH_RUNNER=<runner>`;
2. the default runner chosen by per-user configuration (REQ-FUNC-SELECT-005);
3. the board's default runner.

An override for one runner type SHALL NOT affect the other. A cache value that
Zephyr's board runner defaults treat as unset, such as an empty value, SHALL
NOT count as an override.

### REQ-FUNC-SELECT-013

For a build directory that CMake configured before the module was activated, a
CMake reconfiguration of that build directory with the module active SHALL
register `remote_openocd` under REQ-FUNC-SELECT-001 and generate its default
runners under REQ-FUNC-SELECT-012. Neither SHALL require a pristine build or a
full firmware compile and link. Before that reconfiguration, an ordinary
incremental build or runner command is not required to register
`remote_openocd` or apply its configured default.

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
the user-facing runner option names and value forms exposed by the built-in
`openocd` runner from the particular supported Zephyr 4.4.x environment in use.
Inherited options SHALL preserve their supported, intended effect on runner
configuration and generated OpenOCD or GDB behavior, except for
remote-execution behavior defined by another requirement in this SRS and the
deliberate deviations in REQ-FUNC-OPT-008, whose listed built-in runner
behavior SHALL NOT be reproduced. Identical diagnostics, logging, help text, or
other incidental presentation SHALL NOT be required.

The custom runner MAY add remote-specific options.

Rationale:

This defines a versioned semantic compatibility boundary without duplicating
Zephyr's runner option list or incidental presentation behavior in this
specification, and without requiring bug-for-bug reproduction of the built-in
runner.

### REQ-FUNC-OPT-002

The runner SHALL support probe selection through the built-in `openocd` runner's
`--serial` option in the particular supported Zephyr 4.4.x environment in use.

Rationale:

Remote hosts may contain multiple otherwise equivalent probes.

### REQ-FUNC-OPT-003

If `--serial` is omitted, the custom runner SHALL NOT invent a serial-selection requirement.

### REQ-FUNC-OPT-004

The runner SHALL preserve Zephyr's distinction between the OpenOCD GDB-server
base port (`--gdb-port`) and the target listener selected by the GDB client
(`--gdb-client-port`). For `debug`, `attach`, and `rtt`, forwarding SHALL connect
the local client-selected port to the same remote port, including when it differs
from the base port. For `debugserver`, forwarding SHALL expose the base port
locally at the same port; the unused `--gdb-client-port` SHALL have no effect.

### REQ-FUNC-OPT-006

User-supplied OpenOCD command options inherited under REQ-FUNC-OPT-001 SHALL
retain their intended semantics from the built-in `openocd` runner in the
particular supported Zephyr 4.4.x environment in use when constructing the
remote OpenOCD command, subject to REQ-FUNC-OPT-008.

### REQ-FUNC-OPT-007

The runner SHALL NOT be required to translate arbitrary local paths embedded in arbitrary user-written Tcl.

### REQ-FUNC-OPT-008

The custom runner SHALL deliberately differ from the built-in `openocd` runner
of the particular supported Zephyr 4.4.x environment in use as follows:

- `west flash` of a HEX or BIN image with `--erase` but no erase command SHALL
  fail before remote work rather than succeed without programming the target;
- `west flash` of a BIN image with `--verify` or `--verify-only` but no verify
  command SHALL fail before remote work rather than succeed without verifying
  the target;
- `west flash` with both `--erase` and `--verify-only` SHALL fail before remote
  work rather than ignore the erase request, for any image type;
- `west debugserver` SHALL apply an explicit `--serial` as for the other
  supported commands (REQ-FUNC-OPT-002), rather than ignore it.

Any further deliberate semantic or correctness-motivated difference from the
built-in runner SHALL be added to this requirement. Remote-execution
differences required by other requirements in this SRS are not listed here. A global option selecting literal built-in runner behavior
SHALL NOT be required.

Rationale:

A remote runner cannot let a user observe a silently skipped operation, and
remote hosts commonly connect several probes. Listing each deliberate
difference keeps the compatibility boundary reviewable.

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
Mapping matches SHALL be component-aware: a mapping matches a required local
path when its root is the path itself or one of the path's resolved ancestor
directories. Overlapping mappings with distinct local roots are valid; when a
required local path matches more than one mapping, the mapping whose root is
the nearest such ancestor SHALL take precedence. For example, with `/src` mapped to `/remote/a` and
`/src/board` mapped to `/remote/b`, paths under `/src/board` SHALL use
`/remote/b` and other paths under `/src` SHALL use `/remote/a`. The selected
mapping SHALL translate the path by appending its path relative to the selected
local root to the selected remote root. Mapping destinations and relative path
components SHALL remain literal, including `{workspace}` and `{address}` text;
session-value substitution SHALL apply only at runner-owned substitution points.
An overlapping ancestor mapping SHALL NOT also apply to that path or create a
mapping collision.

Note:

An explicit path mapping is a user-managed remote copy. The runner does not
stage, compare, or verify mapped contents. The selected-image and
configuration compatibility guarantees therefore depend on the assumption in
ASM-012.

### REQ-FUNC-FILE-005

A required local file not covered by an explicit mapping SHALL be staged into the current remote session.

### REQ-FUNC-FILE-006

A required local search directory not covered by an explicit mapping SHALL be
staged while preserving relative structure required by OpenOCD lookup,
including an empty search root and empty nested directories.

The supported staged search-tree input consists of directories, regular files,
and non-cyclic symlinks whose targets resolve within the selected tree. A
symlink that escapes the tree or creates a traversal cycle, and any other
special filesystem entry, SHALL cause staging to fail.

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
intended semantics from the built-in `openocd` runner from the particular
supported Zephyr 4.4.x environment in use, subject to REQ-FUNC-OPT-008, for:

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

The custom runner SHALL NOT launch local GDB or another dependent local client
until remote OpenOCD has reached the runner-generated startup-completion point
and the required forwarding for that operation phase has been established.
At actual execution entry, the local operation SHALL still permit that launch,
with no committed cancellation or established fatal failure. If scheduling
separates launch authorization from execution entry, eligibility SHALL be
checked again at entry. A late readiness report SHALL NOT restore eligibility
after cancellation. These checks do not promise continued process liveness.

For live-server operations, the runner SHALL apply a finite startup-readiness
deadline. Expiration SHALL initiate a bounded final observation of startup
sources owned by the helper, including retained decoded evidence, finitely
available stream bytes, stream EOF, and current child exit state as
applicable. The helper SHALL then choose readiness or startup failure. This is
a finite local observation boundary, not a globally synchronized instant or an
indefinitely extensible drain.

For an operation requiring a live server, OpenOCD exit before readiness,
unestablished readiness after final determination, or failed required forwarding
SHALL fail startup without dependent client launch. A one-shot flash operation SHALL
instead be evaluated from its genuine child result and infrastructure outcome.
The process request SHALL explicitly distinguish these completion policies
rather than infer them from markers or services. One-shot operations SHALL NOT
publish or wait for live-server readiness, or apply its readiness deadline; a
genuine child exit is their process result.

### REQ-FUNC-DEBUG-005

`west attach -r remote_openocd` SHALL connect local GDB without flashing. The
session SHALL allow GDB to read the program counter and the instruction at that
address.

### REQ-FUNC-DEBUG-006

`west debugserver -r remote_openocd` SHALL expose a locally reachable GDB-server
endpoint backed by remote OpenOCD without launching local GDB. The endpoint SHALL
allow an independent GDB client to control the target.

### REQ-FUNC-DEBUG-007

`west debug -r remote_openocd` SHALL preserve the GDB invocation and
initialization behavior of the built-in `openocd` runner from the particular
supported Zephyr 4.4.x environment in use, except for remote-execution
behavior explicitly defined by this specification and the deliberate
deviations in REQ-FUNC-OPT-008.

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
- GDB plus enabled Tcl/telnet for initial `rtt` setup; after initial setup
  succeeds, RTT forwarding is required and GDB forwarding becomes best-effort;
- RTT when the selected operation requests an RTT endpoint.

The selected service set and forwarding requirement SHALL be distinct:

| Operation | Required initially | Required during the client operation | Best-effort forwarding |
| --- | --- | --- | --- |
| `debug` | GDB; RTT when `--rtt-server` is requested | GDB; requested RTT | Tcl, telnet |
| `attach` | GDB; RTT when `--rtt-server` is requested | GDB; requested RTT | Tcl, telnet |
| `debugserver` | GDB; RTT when `--rtt-server` is requested | GDB; requested RTT | Tcl, telnet |
| `rtt` | GDB for initial setup | RTT after initial setup | Tcl, telnet; GDB after setup |
| `flash` | None | None | None |

Required forwarding startup or runtime failure SHALL fail the operation.
Best-effort forwards SHALL be attempted independently, so failure of one
cannot roll back another active forward. Best-effort startup failure SHOULD
warn and allow the required operation to continue only when startup rollback
succeeds. Best-effort runtime failure SHOULD warn at the next forwarding
status check and SHALL NOT terminate an otherwise usable required operation.
Concurrent supervision or interruption of interactive GDB is not required.

For the `rtt` command, forwarding classification SHALL change only after
successful initial GDB setup: GDB is required before the transition, RTT is
required after the transition, and GDB is best-effort after the transition.
Forwarding failures SHALL be classified as required or best-effort when the
runner checks them.

Explicitly requested RTT forwarding for `debug --rtt-server`,
`attach --rtt-server`, and `debugserver --rtt-server` SHALL be required at
startup and throughout the operation. This requirement concerns SSH forwarding
only; the runner SHALL NOT probe the remote RTT service. A local forward does
not guarantee that a
corresponding remote listener is available.

### REQ-FUNC-SVC-002

A disabled OpenOCD service SHALL NOT require a corresponding local listener.
A runner-selected service required by the selected operation or operation phase
SHALL NOT be configured as `disabled`. The runner SHALL reject that
configuration before launching OpenOCD. Disabling optional Tcl or telnet
services remains supported and means that no corresponding local listener or
forward is requested.

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

If a required local service port is occupied by a conflicting listener and the
requested forwarding cannot be established, the operation SHALL fail rather
than silently choose another port. The configured SSH client MAY reuse an
already retained forward with the requested endpoints under REQ-FUNC-SSH-012;
the runner SHALL NOT require a new listener in that case. An occupied
best-effort local port that prevents forwarding SHOULD
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

`west debug -r remote_openocd --rtt-server` and
`west attach -r remote_openocd --rtt-server` SHALL provide GDB and a bidirectional
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

The runner SHALL use a configured external SSH client command satisfying the
capabilities defined in §2.8 for every SSH transport operation.

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
discarded and a local bind failure fails that forward's startup instead of being
ignored. Whether that failure fails the operation is decided by REQ-FUNC-SVC-005
and REQ-FUNC-HELP-011.

### REQ-FUNC-SSH-006

The runner SHALL invoke the SSH argv directly without inserting a shell. A
non-shell launcher that only adjusts inherited process state and replaces
itself with the unchanged SSH argv, without interpreting it, is permitted.

### REQ-FUNC-SSH-009

The runner SHALL NOT require users to duplicate normal SSH credentials, keys, or proxy configuration in the remote-runner configuration.

### REQ-FUNC-SSH-010

When the configured SSH client reports loss of the controlling SSH session,
the runner SHALL record a local operation failure. The runner SHALL report that
failure and begin its bounded local session-cleanup attempt as defined in §3.5
at the next defined session status check. If an active local client is running,
that status check MAY occur after the client returns; the runner is not required
to asynchronously interrupt the local client solely because the loss was
recorded. This local observation SHALL NOT be treated as evidence that the
remote helper has observed control-channel loss or begun remote OpenOCD cleanup.

A locally recorded fatal transport failure SHALL prevent subsequent dependent
local launch and entry into new forwarding work, even if a remote readiness
report arrives later. New forwarding entry SHALL account for already-recorded
terminal outcomes, reader failure, and committed local cancellation through the
same local authority as dependent-client entry. A forward whose entry has
committed MAY finish acquisition or rollback if a terminal fact races with it;
that does not authorize further forwarding work.

Local SSH-loss detection latency SHALL be delegated to the configured SSH
client and the local operating system. This requirement does not impose an
end-to-end bound from the underlying connection loss to local detection.

Completion of the owned local control process SHALL be observed independently
of helper stdout EOF. After observing that completion, the client SHALL give
already-local protocol data a finite nonblocking observation opportunity and
account valid complete frames before selecting a generic transport-end failure.
It SHALL NOT wait for future bytes or EOF. SESSION_CREATED or READY accounted
at that boundary SHALL NOT authorize acquisition or new dependent work on the
ended transport. Without a valid terminal outcome, even zero control status
SHALL be infrastructure failure; nonzero control status SHALL remain independent
failure evidence after a valid terminal outcome. An already-established failure
retains precedence under REQ-FUNC-HELP-011. This boundary defines local
observation, not a total physical ordering of exit and byte arrival.

### REQ-FUNC-SSH-011

The runner SHALL NOT attempt transparent reconstruction of an interrupted debugging session after SSH loss.

### REQ-FUNC-SSH-012

SSH connection sharing, including OpenSSH `ControlMaster`, SHALL remain managed
by the configured client and the user. The runner SHALL NOT disable connection
sharing or require OpenSSH-specific forwarding cancellation commands. Closing
a controller channel SHALL NOT terminate an externally managed sharing master;
a shared channel must still satisfy the directional-EOF contract in §2.8.

Any preferred-address optimization used by the runner SHALL be best effort. The
remote helper SHALL remain authoritative for address leases and service-port
validation; a preferred address SHALL NOT bypass those checks. Missing, invalid,
stale, or inaccessible preference data, inability to reuse a preferred address,
or a retained endpoint mismatch SHALL NOT by itself fail an operation or weaken
helper validation. Concurrent sessions SHALL NOT share an active address lease.
User documentation SHALL explain that externally retained forwards may require
user cleanup.

### REQ-FUNC-SSH-013

When a finite SSH invocation used to prepare an operation, such as remote-home
resolution, helper deployment, the standalone OpenOCD version query, or
staging, exceeds its local timeout, the runner SHALL fail the operation with an
actionable runner error identifying the step and the timeout rather than an
unhandled exception traceback. The timeout SHALL NOT be reported as evidence of
remote completion or cleanup.

Helper startup, from launching the helper control channel until the helper
reports session creation, SHALL be subject to a finite local deadline. On
expiry, the runner SHALL fail the operation in the same way, including
available SSH diagnostics in the error.

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

When the remote helper's lifecycle authority observes controller-input EOF or
a termination signal, it SHALL initiate the bounded process cleanup specified
in REQ-FUNC-HELP-012. If termination or disposal cannot be confirmed within that
attempt, it SHALL record unsuccessful cleanup and report it when output remains
usable. Transport loss MAY prevent delivery. Remote SSH/operating-system
detection latency is outside the cleanup bound. Local detection SHALL NOT be
treated as remote observation, and the interval between them is not bounded.

A native SIGINT or SIGTERM received by the remote helper SHALL be a
helper/session failure under either completion policy, even if disposal succeeds
or the child result is zero. It SHALL establish a failure if none exists or be
retained as a secondary diagnostic under REQ-FUNC-HELP-011. A signal observed
during already-initiated shutdown SHALL remain a failure without replacing its
initiating cause; successful disposal SHALL NOT mask that failure. If the terminal outcome is already frozen, the
signal failure SHALL remain visible through nonzero helper status without
replacing that outcome. Intentional local closure releases the controller
lease under REQ-FUNC-HELP-015; helper-issued child-disposal signals SHALL NOT by
themselves constitute this helper failure.

The helper SHALL continue observing controller termination while readiness is
pending. Bulk output backpressure SHALL NOT prevent controller or signal
observation, child exit accounting, or cleanup progress. Readiness MAY precede
the helper's recognition of controller-input EOF, and MAY precede remote
handling of a released EOF. Readiness SHALL NOT be reported after the helper
has recognized controller-lease loss (REQ-FUNC-HELP-015). Once remote
termination is committed, no new attempt or readiness may be authorized. Local
cancellation independently prevents dependent launch.

### REQ-FUNC-HELP-006

The client and helper SHALL independently validate their session-contract
boundaries before acting on input. For a requested live-server operation, the
helper SHALL report readiness only for the current owned live child after
required startup evidence and final live-child validation. For that policy,
the helper SHALL NOT declare the session active if it cannot accept the
readiness report for delivery. Readiness does not itself authorize a local
client launch.

Remote OpenOCD stdout/stderr SHALL be relayed incrementally while the child is
running, including long newline-free output, while transport remains usable.
Ordering SHALL be preserved within each stream; no total order between streams
is required. Protocol and output memory SHALL remain bounded. Final retained
output SHALL receive a finite drain opportunity after resource cleanup; drain
failure SHALL NOT undo completed cleanup.

The helper SHALL commit at most one immutable terminal outcome, independently
representing the initiating trigger, genuine child result if observed,
established primary failure, ordered secondary diagnostics, and cleanup or
residual responsibility. Orderly closure and helper failure SHALL remain
distinguishable in that outcome. Failure to deliver it SHALL NOT create another
terminal outcome. Local diagnostics MAY grow after its wire snapshot is frozen.
Loss of transport MAY prevent delivery of the final outcome.

Exact message fields, framing, validation, and frame-size limits belong solely
in [protocol.md](../architecture/protocol.md). Automatic deployment SHALL
provide the helper revision matching the local client; a version value alone
does not establish compatibility with the full contract.

### REQ-FUNC-HELP-007

Concurrent helper deployments SHALL be safe: they SHALL NOT expose a partial
helper revision or remove the revision selected by an active deployment.
Deployment SHALL attempt opportunistic reclamation of eligible unselected
helper revisions. Failure to inspect or remove an eligible revision SHALL NOT fail
deployment or affect the selected revision.

### REQ-FUNC-HELP-008

Service configuration SHALL be validated before process startup. The client
and helper SHALL independently validate the portions of the service contract
available at their respective boundaries. The exact request fields and
validation rules are defined in
[`protocol.md`](../architecture/protocol.md).

This validation applies to the runner-selected service and forwarding
configuration; it does not
discover or validate the effective service state produced by arbitrary OpenOCD
Tcl.

### REQ-FUNC-HELP-009

The client SHALL preserve a genuine OpenOCD child result reported through the
valid helper protocol, independently of the initiating shutdown trigger.
The result SHALL originate only from observing that child. Helper-process,
SSH/control-transport, and forwarding-process statuses, protocol failures,
cleanup failures, and timeouts SHALL NOT become OpenOCD exit statuses.
Requested termination without an observed child result SHALL NOT synthesize one.
Natural exit and controller termination MAY race, and either may initiate
shutdown; the actual observed child result SHALL retain its provenance.
Observed status SHALL retain whether helper-requested child termination preceded
its observation. Normal disposal of a server after successful local client work
SHALL NOT fail that work solely because the disposed child has a nonzero status;
an independent natural child failure or infrastructure failure remains significant.

### REQ-FUNC-HELP-010

Session shutdown SHALL be idempotent and attempt every independently owned
cleanup action under the bounded policy in §3.5. Interruption or one cleanup
failure SHALL NOT abandon other independently owned resources. Successful retry
of a partially failed cleanup sequence is not required. Failure to release an
owned resource SHALL remain visible under REQ-FUNC-HELP-011, with unconfirmed
disposal and residual responsibility represented explicitly.

Coordinated local shutdown SHALL prevent new dependent launch, close helper
stdin, continue reading final output and outcome, and wait within a finite
coordination budget for terminal information and owned SSH-process settlement.
It SHALL escalate termination of the owned local transport if necessary.
Transport/helper status SHALL remain independent of the remote outcome.
A missing terminal outcome or a local shutdown timeout SHALL report infrastructure
uncertainty, not remote cleanup success, even if the helper or SSH status is zero.

After transport loss, each side SHALL attempt cleanup of its owned resources
after its own observation. Local completion SHALL NOT certify remote disposal.

Locally owned transport resources are the SSH subprocesses launched by the
runner and their owned pipes, readers, and diagnostic drains. Forwarding state
retained by an external connection-sharing mechanism, including its listeners
and master process, is outside this ownership boundary. Such retained state
SHALL NOT by itself be reported as failed runner cleanup. Failures to clean up
runner-owned resources remain subject to REQ-FUNC-HELP-011. Remote helper leases,
OpenOCD supervision, and workspace cleanup remain runner-owned.

### REQ-FUNC-HELP-011

When an operation failure has already been established, later cleanup
failures, session/infrastructure failures, or OpenOCD-result observations
SHALL NOT replace that failure. Later failures and relevant OpenOCD results
SHALL remain available as ordered diagnostic information, including nested
secondary cleanup detail. No total ordering between unrelated failures is
required before a primary failure has been established. When no earlier failure
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
helper's owned-process supervision contract.

The remote session helper SHALL own the OpenOCD process and its descendants for
cleanup. Once the helper's lifecycle authority observes controller termination,
it SHALL begin a bounded cleanup attempt as defined in §3.5 for those
owned processes and associated session resources. Cleanup SHALL attempt to
terminate the owned OpenOCD process and descendants and release the OpenOCD
leader and owned relay resources. Diagnosis of surviving descendants when
observable SHOULD be provided, but failure of best-effort descendant
inspection SHALL NOT by itself make otherwise successful cleanup fail. The exact
ownership boundary and signal, wait, inspection, escalation, reaping, and
relay-cleanup algorithm belongs in the SAD. Successful termination is required
when the termination and reaping operations complete successfully within the
bounded cleanup attempt. Otherwise, the attempt is unsuccessful and
conformance requires the cleanup-failure recording and reporting specified by
REQ-FUNC-HELP-010 and REQ-FUNC-HELP-011; a failed attempt is not required to
guarantee that every descendant has terminated.

Acquired process and relay resources SHALL remain continuously reachable by a
cleanup owner, including partial construction and interruption immediately
before or after ownership transfer. Child leader exit SHALL NOT by itself
establish settlement of descendants, descriptors, or acquisition producers.

Runner-owned local Ctrl-C SHALL NOT raise into session-owned process creation,
ownership handoffs, or cleanup; it is recorded and acted upon only as defined by
REQ-FUNC-HELP-013. During remote session-owned process creation in
standard-library/native code, the remote helper's supported handled signals
SHALL be deferred until the runtime publishes a usable process cleanup handle.
At project-controlled ownership handoffs and setup after `Popen` returns,
rollback SHALL cover any `BaseException`.
This contract does not require recovery from arbitrary exceptions synthetically
injected inside CPython before it publishes the process cleanup handle.

Local session and RTT connection acquisition SHALL populate a resource-free
owner already held within the caller's cleanup scope. Acquisition SHALL NOT
transfer a newly live session or connected socket to the caller through a
return value; callee rollback alone does not protect interruption before caller
adoption.

### REQ-FUNC-HELP-013

During a live `flash`, `debug`, `attach`, `debugserver`, or `rtt` operation,
Ctrl-C owned by the runner SHALL be recorded as a durable cancellation request
for that operation. Recording SHALL NOT itself raise into, interrupt, or abandon
an in-progress resource acquisition, ownership handoff, or cleanup step.
Repeated Ctrl-C SHALL leave the request recorded and SHALL NOT change cleanup
policy, shorten cleanup deadlines, or skip independent cleanup actions.

A recorded request SHALL take effect promptly while the runner waits on remote,
network, or local-process activity, including finite SSH commands, helper
startup and readiness, START delivery, forwarding readiness and status checks,
the foreground server or one-shot result wait, RTT connection and relay, and
batch GDB setup. Once recognized, it SHALL prevent new dependent work, such as
another remote command, forwarding, the remote process, or a local client, from
starting. The runner is not required to interrupt synchronous local CPU or
filesystem work, such as staging-archive generation; a request received during
such work SHALL be retained and acted upon at the next wait or work-admission
point. This requirement does not impose a wall-clock latency on individual
operating-system, filesystem, or terminal operations.

When cancellation takes effect after session resources are acquired, the runner
SHALL report once that remote cleanup is in progress and SHALL initiate bounded
session closure under REQ-FUNC-HELP-010, covering resources acquired so far,
including during startup. Cleanup failures SHALL remain visible under
REQ-FUNC-HELP-011. When cancellation is the operation's primary outcome, the
runner SHALL finish with the host Python runtime's normal interrupted-command
behavior (`KeyboardInterrupt`) after cleanup. An earlier established primary
failure SHALL remain primary, with the cancellation retained as diagnostic
information.

During `debug` or `attach`, the configured GDB client invocation, including any
configured wrapper, SHALL own terminal Ctrl-C with the normal GDB interaction
semantics of the built-in `openocd` runner from the particular supported Zephyr
4.4.x environment in use. Such an interruption SHALL NOT by itself cause the
runner to cancel the operation or close the remote session. A cancellation
recorded before the client is launched SHALL prevent its launch. Independently
observed session or transport failures SHALL still be handled under
REQ-FUNC-HELP-011.

Runner-owned Ctrl-C applies when the runner executes on the main thread of the
host Python process with SIGINT dispatched to a Python-level handler. A process
started with SIGINT ignored or at its native default disposition retains that
disposition.

### REQ-FUNC-HELP-014

The deployed helper SHALL attempt child creation at most once per START. Neither
a bind diagnostic nor a genuine post-spawn bind collision establishes safe
repeatability: opaque/user Tcl and target initialization may already have run.
Pre-spawn address selection and rejection MAY continue without executing child
work again.

### REQ-FUNC-HELP-015

Intentional local closure of a started session SHALL release the controller
lease explicitly before ending controller input. The helper SHALL classify
controller-input EOF when it recognizes that EOF: after an earlier explicit
release on the same input stream it is orderly session closure; otherwise it
is controller-lease loss and SHALL fail the session. Lease loss recognized by
the helper SHALL be retained for its lifecycle authority independently of
queued observation delivery and accounted before readiness is reported and
before the terminal outcome is frozen. If another cause has already initiated
termination, that cause SHALL be retained and the lease loss reported as a
failure under REQ-FUNC-HELP-011. EOF that the helper has not recognized when
its outcome is frozen is not required to fail the session.

The client SHALL take this classification from the helper's terminal outcome.
The time that outcome is received, relative to local closure, SHALL NOT change
it.
Release SHALL NOT require an acknowledgement, a wait for unread input, or more
than one attempt. Inability to deliver the release SHALL NOT by itself replace a
terminal outcome the helper reports, nor make a missing or failed outcome
successful. Input framing SHALL tolerate arbitrary transport fragmentation and
coalescing.

---

## 23. Session Data

### REQ-FUNC-DATA-002

Remote session files SHALL be protected from other ordinary remote users by filesystem permissions.

### REQ-FUNC-DATA-003

Normal session termination SHALL attempt to remove temporary session artifacts
once their dependent users have settled.
Successful cleanup SHALL leave no temporary session artifacts. If removal
fails, the failure SHALL be reported according to REQ-FUNC-HELP-010 and
REQ-FUNC-HELP-011.

Cleanup SHALL NOT remove a workspace while staging operations already using it
are validating or extracting their archives or reporting success. Once cleanup
begins, new staging operations SHALL NOT use or recreate that workspace, even
if cleanup fails or the workspace has been deleted. A stalled staging operation
SHALL NOT prevent a bounded cleanup attempt as defined in §3.5 or prevent
cleanup of independent process resources or other sessions. Inputs SHALL remain
available while an existing staging operation, acquired child, or unresolved
acquisition producer can legitimately use them. Workspace removal SHALL require
confirmed dependent disposal and safe exclusion of staging. Unconfirmed child
disposal SHALL retain dependent workspace data and report cleanup failure or
residual responsibility. Cleanup failures SHALL remain visible and SHALL NOT
permit new staging operations to resume use of the workspace.

### REQ-FUNC-DATA-005

The helper SHALL protect active session workspaces and staging operations. It
SHALL attempt opportunistic reclamation of session workspaces and orphaned
coordination metadata that it can safely establish are inactive or orphaned.
If eligibility cannot be safely established, or if inspection or removal fails,
the helper MAY leave the entry for a later allocation. Such failure SHALL NOT
fail allocation or require another reclamation attempt during the same
allocation.

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

Routine runner use, helper installation and deployment, helper execution,
upgrades, cleanup, and diagnostics SHALL NOT require root privileges.

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

Firmware, configuration, and staged search-tree artifacts are sufficiently
small that a persistent artifact cache is unnecessary.

### ASM-008

The configured remote SSH account can execute the configured OpenOCD executable
and Python, and can create the per-user helper and session state required by the
selected operation.

### ASM-009

The configured remote SSH account has permission to access the selected debug
probe or channel.

### ASM-010

The SSH service permits the local TCP forwarding required by the selected
operation.

### ASM-011

The configured remote OpenOCD installation is assumed to support the commands,
configuration files, debug adapter, target, and network services required by
the selected Zephyr OpenOCD runner operation.

The runner does not attempt general OpenOCD feature discovery or certify
compatibility of arbitrary OpenOCD versions or vendor forks.

### ASM-012

Explicit path mappings are assumed to point to remote destination contents
equivalent to the corresponding local inputs, including mapped firmware,
OpenOCD configuration, and search-tree contents. The runner does not stage,
compare, or verify mapped contents.

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

Keep all use of `OpenOcdBinaryRunner` in the Zephyr 4.4.x compatibility layer.
Do not use private attributes or methods. Validate or update the runner
integration for each newly supported Zephyr version.

### RISK-007 - SSH client differences

External SSH clients can differ in process behavior, authentication, and
forwarding behavior.

Severity: Medium.

Mitigation:

Depend only on the capabilities defined in §2.8 and preserve configured argv.
