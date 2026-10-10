# Zephyr Remote OpenOCD Software Architecture Document

## Navigation

- [Purpose, drivers, and overview](#1-purpose)
- [Module structure and setup](#5-self-contained-zephyr-module)
- [Zephyr runner integration](#9-module-discovery)
- [Remote execution planning](#18-generic-remote-session-model)
- [Services, SSH, staging, and lifecycle](#27-remote-openocd-service-isolation)
- [Errors, code boundaries, tests, and decisions](#42-error-handling)

## 1. Purpose

This document describes the selected architecture for the Zephyr west runner
for remote OpenOCD. The controller-lease lifecycle and Protocol v2 are the
redesign target, not yet the deployed helper behavior. Structured remote and
local lifecycle foundations and physical-owner adaptation are implemented.
Production remains on Protocol v1 until client/helper cutover;
existing physical ownership mechanisms described here are retained or adapted,
not replaced by experimental implementations. The implementation gaps are
tracked in [verification.md](../traceability/verification.md#controller-lease-migration).

The SRS defines externally required behavior.

This document describes implementation structure and architectural decisions.

---

## 2. Architecture Drivers

The primary drivers are:

- no development-repository changes;
- board-agnostic design;
- one Linux implementation;
- Zephyr 4.4.x compatibility;
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

## 4. Supported Platforms

The local platform is Linux with Python 3.12 or newer. The remote helper
platform is also Linux with Python 3.12 or newer. The generic local Python
implementation uses one Linux architecture and does not select platform
backends. Linux-specific remote process supervision and filesystem mechanisms,
including process groups, signals, pidfds, `/proc`, and `fcntl` locking, belong
to the helper and supervision layer. Setup and generic runner code avoid adding
further Linux-specific assumptions, so future native Windows work can remain
localized; native Windows execution is outside the current scope.

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
                preferred_address_cache.py
                openocd_plan.py, arguments.py, tcl.py

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
jsonschema for configuration loading. The configured Python environment for the
supported Zephyr 4.4.x environment already provides these accepted runtime
dependencies; they are not functionality to reimplement. Setup reports
discoverability of `pyelftools`, PyYAML, and jsonschema in the active Python
environment. Missing dependencies warn and
direct users back to that environment without preventing configuration
initialization. The module does not require pip packaging or a separate
dependency installation path.

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
loader safely parses YAML with duplicate-key rejection, then uses `jsonschema`
at runtime to validate the parsed document against that canonical schema. The
schema is the sole source for the structural and lexical rules it expresses;
those rules are not duplicated in handwritten validators. It rejects explicit
nulls and other structural and lexical violations, including invalid command
and path spelling, NULs, and invalid normalized remote paths. Code then
expands and resolves local mapping keys and detects collisions because those
operations depend on the local filesystem. Remote references and required
`openocd_command` are checked only when the selected remote is used, allowing
incomplete unused definitions.

Remote fields replace complete preset settings; lists and mappings are not
merged. During a real operation, a home-relative `openocd_command` executable
and home-relative remote path-mapping destinations are resolved using the SSH
user's actual home; recording keeps them unresolved.

### Configuration Resolution

Runtime loading treats an absent configuration path as an empty configuration.
Malformed YAML or schema-invalid content, and an existing path that cannot be
read as a file, produce an actionable configuration error rather than falling
back to defaults. For a production operation, remote selection is ordered as
explicit `--remote`, then a non-empty
`ZEPHYR_REMOTE_OPENOCD_REMOTE`, then `default_remote`. The selected remote must
exist; production resolution also requires `openocd_command`. Resolution uses
the built-in defaults of `ssh_command: [ssh]`, empty `forward_env`, empty
`path_mappings`, and `ssh_host` equal to the remote name when those settings
are not supplied.

Offline validation intentionally uses a different selection and file-presence
policy. It resolves explicit `--remote`, otherwise the file's
`default_remote`, and ignores `ZEPHYR_REMOTE_OPENOCD_REMOTE`; its target file
must exist so that validation cannot silently summarize an absent file.

`scripts/user/validate_configuration.py` is an offline front end to this loader and
resolver. A non-empty `ZEPHYR_REMOTE_OPENOCD_CONFIG` overrides the product
default configuration path, and a leading current-user `~` is expanded before
the file is read. With no selected remote it reports structural validity and
available definitions. It prints commands as argv and forwarded environment
names without values, and does not test local or remote resource existence.

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

and mirrors the board-specific runner arguments generated for the built-in
`openocd` runner.

For a build without `openocd`, the module does not add `remote_openocd`.

No board name, vendor name, architecture, or SoC family participates in this decision.

Eligibility is based exclusively on existing OpenOCD runner support.

---

## 12. Common Runner Configuration

Common `RunnerConfig` data is reused directly.

The reused common fields are the following when provided by Zephyr:

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

For builds that register `openocd`, the CMake compatibility layer mirrors the
board-specific runner arguments generated for:

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

For an OpenOCD-capable build, during CMake configuration:

```text
openocd        -> openocd
remote_openocd -> remote_openocd
```

is written into generated flash/debug runner defaults. These defaults are
generated runner-selection metadata; they do not claim that a target, probe,
or OpenOCD service is reachable.

The generated setting is only the default. Normal west `-r openocd` or
`-r remote_openocd` selection remains available and takes precedence when
explicitly supplied.

The product's `default_runner` setting has two supported values:

```text
openocd
remote_openocd
```

---

## 15. Automatic Default Regeneration

Changes to the contents, creation, or deletion of the configuration file at the
path selected when CMake last configured the build are detected by the normal
incremental build and reconfiguration machinery. Changes affecting generated
runner state are applied without requiring a pristine build or a full firmware
compile and link. By default, the configuration path is:

```text
~/.config/zephyr_remote_openocd/config.yaml
```

A non-empty `ZEPHYR_REMOTE_OPENOCD_CONFIG` selects the configuration file used by
each process that loads configuration. During CMake configuration, that path is
also selected for build-time change detection and generation of runner defaults.
Relative selections are resolved against the initial CMake invocation directory.
The absolute file identity is retained in the build cache while the selection
input is unchanged, so regeneration from the build directory watches and loads
the same file. Content dependencies, presence tracking, and the Python loader
all use that identity.
Changing the override to select another file is a configuration-input change:
CMake must be reconfigured before generated runner state is expected to reflect
that path. An ordinary incremental build does not detect an environment-only
path switch. The runner may therefore load configuration from the new path while
the generated default still reflects the previous CMake configuration.

Expected flow:

```text
selected file contents/creation/deletion changes
      -> ordinary incremental build or CMake reconfiguration
      -> updated runners.yaml

selected path changes via ZEPHYR_REMOTE_OPENOCD_CONFIG
      -> explicit CMake reconfiguration
      -> updated runners.yaml

west flash/debug consumes generated state
```

Normal runner commands trigger their usual pre-run build, so changes to the
selected file are picked up after the module is configured into the build.
Explicit `west build` also detects these file changes. After changing the
selected path for an existing build directory, run a CMake reconfiguration such
as `west build --cmake-only -d <build>` before relying on the generated default
runner. When rebuilding or regeneration is explicitly suppressed, such as with
`--no-rebuild`, existing generated state may remain until a later regeneration.

---

## 16. Zephyr Runner Reuse Strategy

The runner may subclass and reuse the non-private interface of the built-in
`openocd` runner from the particular supported Zephyr 4.4.x environment when
doing so reduces duplication.

The compatibility policy is:

> Zephyr supports `runners.core` as its external-runner API. It does not make
> that compatibility guarantee for `OpenOcdBinaryRunner`. The Zephyr 4.4.x
> runner integration may use the class's non-private interface, but that code
> remains version-specific and confined to the Zephyr compatibility layer.

The built-in `openocd` runner from the particular supported Zephyr 4.4.x
environment in use defines the compatibility boundary for the supported west
commands. The
adapter preserves the functional effect of inherited options while translating
them into remote plans, except where a specific SRS requirement defines
different remote behavior. In particular, the remote runner owns the remote
bind address and service and forwarding configuration because it must allocate
remote ports and construct SSH forwards before local clients can use them.

The Zephyr 4.4.x runner integration reuses `capabilities()` and the constructor.
It overrides
`name()`, `do_create()`, `do_add_parser()`, and `do_run()`. The parser override
delegates to `OpenOcdBinaryRunner.do_add_parser()` before adding `--remote`.
Constructor and version coupling is isolated in `zephyr44/runner.py`.

Supporting a new Zephyr release requires validating this interface or updating
the version-specific runner integration.

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

The Zephyr adapter passes `--serial` as structured probe-selection state to
the shared OpenOCD command planner in `remote/openocd_plan.py`, used by flash,
debug, attach, debugserver, and RTT. When supplied, the planner emits
`-c "set _ZEPHYR_BOARD_SERIAL <serial>"` before the board configuration `-f`
arguments, so those configurations can consume the variable while loading.
When `--serial` is omitted, the planner emits no serial-selection command and
introduces no serial constraint.

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

### 18.1 Runtime Environment Forwarding

While constructing the immutable `RemoteProcess` for an operation, the
Zephyr adapter reads local values only for names in the selected remote's
`forward_env` allow-list. The complete local environment is never copied. A
name with no local value causes a non-fatal warning and is omitted from the
process-start request; this does not remove a same-named value from the
helper's inherited remote environment.

The selected values travel with the process plan into the protocol command.
The helper copies its inherited environment and overlays those requested values
before spawning OpenOCD, so allow-listed values are available while OpenOCD
processes configuration files. The command's exact field and validation rules
are defined solely in [protocol.md](protocol.md).

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

When a required path matches more than one mapping, the mapping with the
longest normalized local root takes precedence. The selected mapping translates
the path by appending its relative suffix to the mapped remote root; other
matching mappings do not also apply. If no mapping matches, the path is staged
into the current session.

---

## 20. OpenOCD Search Trees

Large OpenOCD search trees which exist equivalently on both systems may be explicitly mapped.

Search paths supplied by Zephyr are preserved.

Relative `-f` configuration references are resolved locally before file planning:
first against the current working directory, then against the supplied `-s`
directories in their original order. The selected file goes through the same
mapping or staging logic as an absolute configuration path. Search trees may be
staged in parent-first order to reuse overlapping roots, but that staging order
does not change configuration lookup or the remote `-s` argument order.

When a search directory is staged, the local path planner resolves each entry
before building the archive. A symlink whose target remains within the selected
tree is followed and represented as the target's ordinary file or directory
entry. A link that escapes the selected tree or creates a traversal cycle is
rejected, as are filesystem entries that are neither directories nor regular
files. The resulting archive therefore contains no symlink members. This local
source-tree policy precedes the helper's independent archive validation, which
also rejects symlink, hard-link, and special-file members from any archive
producer.

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

Only firmware/configuration inputs and search trees selected through the
supported OpenOCD input boundary are mapped or staged. A selected search tree
may contain files that OpenOCD does not ultimately consume. GDB, the toolchain,
and the authoritative local development artifacts—including source files,
debug symbols, and local build output—remain on the development host.

Flash command construction is phase-oriented: a shared immutable OpenOCD
prefix is combined with a resolved image plan and one concrete ELF, BIN, or
HEX operation plan. The public flash-plan result remains the runner boundary.
Explicit erase commands are resolved and validated once for the selected
operation, then consumed by command construction. Only HEX/BIN load operations
with erase requested require them; ELF and verification-only operations omit
explicit erase commands, matching the inherited Zephyr behavior.

Planning distinguishes literal strings from explicit runner-owned session-value
references. Mapped paths are literal; staged paths carry a workspace reference
followed by a literal relative suffix. Generated bind commands carry an address
reference. Neither a mapping destination nor a filename becomes a template
because it contains `{workspace}` or `{address}`. Inherited Tcl and the
configured command prefix also remain literal.

Generated firmware Tcl arguments retain the path separately from surrounding
command text in immutable argument templates. The helper materializes these
templates for each attempt and quotes each resolved firmware path as one Tcl
word before reporting and spawning the exact effective argv. Values introduced
by workspace or address resolution are not scanned again. Offline argv previews
may show unresolved session references; execution uses the explicit templates,
not inference from preview text. Required-path checks preserve the same
literal/session-reference distinction without applying Tcl quoting. The
process-start wire details remain defined solely in [protocol.md](protocol.md).

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
wait for OpenOCD startup readiness
     |
     v
establish required and best-effort forwarding
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

`debug`, `attach`, `debugserver`, and `rtt` plan construction similarly separates immutable service/RTT
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
establish required GDB; independently attempt best-effort Tcl/telnet
       |
run local batch GDB for west rtt
       |
       +-- RTT setup
       +-- RTT start
       +-- RTT server start <port>
       |
mark GDB best-effort; establish required RTT transport
       |
launch local RTT client
```

The RTT forward's local listener does not prove that its remote channel opened.
The active RTT client must connect to establish end-to-end reachability.
The dedicated standard-library client provides
bidirectional channel-0 bytes, uses noncanonical/no-echo TTY input without
disabling normal signal handling, and restores the complete terminal state on
every exit path. Non-TTY input is supported without terminal operations.

Connection setup owns each acquired socket through its initial readiness check
and closes it on failure or interruption. The client's cleanup scope is active
before adopting the connected socket, accessing stdin, or delivering initial
output. A terminal-mode scope restores prior attributes before the socket is
closed. The relay owns the bounded pending-input queue and interprets readiness,
retaining unsent bytes across partial writes and transient backpressure. It polls
session state before each readiness wait and again at receive-side channel EOF,
so a recorded session exit takes precedence over that channel closure. Terminal
and socket cleanup are attempted independently; an existing failure remains
primary, with secondary cleanup failures attached as notes.

For `debug --rtt-server`, `attach --rtt-server`, and `debugserver --rtt-server`,
RTT setup is included in OpenOCD's startup command sequence before its
startup-complete marker.
OpenOCD owns the RTT listener; the helper does not probe it. These operations
expose the endpoint but do not launch a local RTT client. The `rtt` command reuses the
same remote OpenOCD version and Zephyr thread-info decision as debug/attach.
No GDB RSP observer is needed.

GDB forwarding is required during the `rtt` command's batch setup. After setup succeeds,
the Zephyr runner integration explicitly marks the owned GDB forward best-effort
and starts the deferred RTT forward as required. A GDB exit first observed
after this transition produces a warning; an RTT-forward exit remains fatal.
Explicitly requested RTT forwarding for `debug --rtt-server`,
`attach --rtt-server`, and `debugserver --rtt-server` is required alongside GDB
at startup and throughout the operation. The planner classifies only enabled
Tcl/telnet forwards as best-effort. GDB and requested RTT start in the same
required forwarding batch;
RTT forwarding startup failure aborts session opening, and runtime failure is
fatal at the next forwarding status check. During interactive GDB that check
may occur after GDB returns; concurrent interruption is not required. This
policy concerns SSH forwarding only and adds no RTT service probe. RTT
forwarding is reported established only after its local forward starts successfully.

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

The helper allocates each remote session a leased loopback address from:

```text
127.64.0.0/10
```

Different sessions therefore may use identical service-port numbers without collisions.

For sessions selecting services, the local runner offers the last address
remembered for the configured SSH argv prefix and host. The helper tries that
preference first with the same address lease and initial/deferred port checks
as random candidates. If it is unavailable, allocation falls back to up to 32
random candidates. A child bind-collision retry uses random allocation without
retrying the preference. A cached address conveys no ownership and cannot
override another session's lease.

The optional preferred candidate is additional to the 32-candidate random
allocation budget. Exhausting that budget fails startup without launching a
child for that allocation attempt. This candidate budget is distinct from the
child startup-attempt limit described in §38.

The helper reserves each candidate address by binding a Linux abstract Unix
socket whose name is keyed solely by that address. The kernel socket namespace
coordinates helpers across remote users, runtime directories, and helper
revisions within the same network namespace. Separate network namespaces have
independent loopback networks and independent leases. The helper temporarily
binds all initial and deferred service ports to check for collisions, then
releases the probe sockets before starting OpenOCD. The address lease remains
held until child cleanup completes, including when RTT forwarding is deferred;
it is released before a retry selects a new address and during session cleanup.
The kernel also releases it if the helper exits or is killed.

OpenOCD owns the actual enabled GDB, Tcl, telnet, and RTT listeners on the
allocated address; the helper neither creates nor probes those listeners.
The lease excludes other cooperating helpers, while unrelated processes remain
able to bind TCP ports. An OpenOCD bind failure remains an operation failure
and may cause a fresh leased address to be selected for a startup retry.

Flash requests no services and therefore creates no local forwards. `debug`,
`attach`, and `debugserver` request GDB and each non-disabled Tcl/telnet
service. The `rtt` command requests GDB plus each enabled Tcl/telnet service
for batch setup and reserves its deferred RTT service for helper-side address
validation; after batch GDB setup, RTT is required and GDB becomes best-effort.
RTT is selected and required for `debug --rtt-server`, `attach --rtt-server`, and
`debugserver --rtt-server`. This service and forwarding configuration is
derived from the operation and runner options, not
from runtime discovery of the effective OpenOCD configuration.

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

The runner does not request local forwarding for disabled services. An external
sharing mechanism may still retain a listener from an earlier operation.

The forward manager owns the local SSH-forward subprocesses it launches and
their pipes and diagnostic drains. When the client uses external connection
sharing, its master may own and retain local loopback listeners independently
of those subprocesses. The runner does not own that externally retained
forwarding state. The corresponding remote listeners remain owned by
OpenOCD. A successfully created local forward proves only that SSH accepted the
forward; it does not prove that OpenOCD has a listener behind the remote
endpoint.

`RemoteSessionRequest.services` describes the initial service set supplied to
the helper and forwarded while the session opens. `reserved_services` adds
service ports that the helper validates and includes in address allocation but
that are forwarded later by the operation. Its `auxiliary_services` subset
identifies initial best-effort forwards; all other initial service forwards are
required. The `auxiliary_services` name is an internal implementation detail
for this client-side classification; it is not part of the wire contract
described in [protocol.md](protocol.md). Omitting the subset preserves the
generic all-required default. Debug plans classify enabled Tcl/telnet as
auxiliary and GDB plus explicitly requested RTT as required. RTT for the `rtt`
command remains reserved but deferred in the debug plan and is forwarded only
after batch GDB setup succeeds.

Immutable local `Service` and `RemoteSessionRequest` construction validates
the locally knowable service contract before the helper request is emitted,
including service names, ports, duplicates, conflicts, and auxiliary or
reserved-set relationships. The helper independently validates its wire-side
service contract as defined in [protocol.md](protocol.md).

The session starts required forwards in one batch, then attempts each
best-effort forward in its own one-service batch. Each manager call rolls back
that call's pending processes and preserves previously active forwards.
`ForwardStartError` exposes the failed service, startup cause, and explicit
rollback cleanup errors. The session warns only when a best-effort attempt
rolled back successfully. Failed rollback remains fatal; pending processes
receive one bounded cleanup attempt and need not be adopted for a retry.
The manager owns the pending batch from transport registration through commit
or completion of each rollback attempt. Pending transports remain reachable by
`close()` if startup escapes before rollback's SIGINT mask is established.
They are not published as active services, and another startup cannot begin
until a stranded pending batch has been cleaned. Close covers committed and
pending ownership, attempting a transport once if an interrupted commit has
temporarily recorded it in both sets.

Forward startup blocks SIGINT only while registering each returned transport in
the pending rollback set and while committing both the process and service
lists. Commitment relinquishes pending rollback ownership before restoring the
caller's previous signal mask. At every point where SIGINT can be delivered,
each acquired forwarding process belongs to either the startup rollback set or
the manager's committed ownership set. Readiness waits retain the caller's
previous mask, so startup remains interruptible.

Failed startup defers SIGINT through the complete pending rollback batch,
including the intervals between process cleanup attempts. It then restores the
caller's exact prior mask. A pending interruption is retained behind the startup
failure only after all pending transports have received their cleanup attempt.

The manager reports newly observed exits with service identity and transport
diagnostics. The session retains those facts, classifies them as required or
best-effort, and emits each best-effort runtime warning once. Required failures
remain fatal on repeated status checks. Structured `ForwardAdvisory` values
reach an injected callback; the Zephyr runner integration formats them through
its normal warning logger. Cleanup attempts every active process, and failure
to clean up an acquired resource remains fatal regardless of forwarding type.

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

The configured prefix separates the SSH executable from fixed user arguments.
Helper and staging operations append the host and remote command unchanged.
The resulting SSH argv is executed directly without inserting a local shell.
Forwarding operations pass an immutable `SshLocalForward` to `SshCommand.popen()`;
the invocation boundary renders its loopback `-L` together with
`ExitOnForwardFailure=yes` and `ClearAllForwardings=no` immediately after the
executable, before fixed user arguments. OpenSSH's first-value precedence makes
these runner-owned requirements override conflicting arguments and normal SSH
configuration. The forward manager cannot request a local forward without
these mandatory settings.

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

This permits users to select another external SSH executable and rely on
the configuration, credentials, and agent behavior provided by that client.

---

## 31. SSH Transport Capability Model

Correctness shall depend only on the SSH capabilities defined in SRS §2.8.

Conceptually:

```text
required:
    execute remote command
    stdin/stdout/stderr streaming
    directional EOF with reverse output retained
    TCP forwarding with generated local-forward support
    runner-owned forwarding-option precedence
```

The configured client must preserve the runner's generated `-L` forwarding
arguments and honor first-value precedence for `ExitOnForwardFailure=yes` and
`ClearAllForwardings=no`. The detailed forwarding construction and precedence
requirements are defined in §29.

For the long-lived helper, closing local stdin must deliver remote stdin EOF
while stdout/stderr remain usable until remote command exit. Stdin-suppressing,
bidirectionally closing, incompatible PTY, or detaching wrappers do not satisfy
this contract. There is no STOP fallback, capability negotiation, or new YAML
setting for them. Under connection sharing, EOF closes only the controller
channel, leaving the external master alive.

---

## 32. SSH Connection Sharing

The runner does not manage or require SSH connection sharing. Multiple SSH
processes are used by the runner; the configured SSH client may multiplex them
according to its normal configuration.

The runner neither disables sharing nor adds OpenSSH-specific `-O cancel`
handling. Its cleanup boundary ends at the SSH subprocesses and local I/O
resources it acquired; a sharing master and retained forwards remain externally
managed. Subprocess exit therefore does not prove that a shared listener has
been removed, and retained external state alone is not a runner cleanup failure.

`remote/preferred_address_cache.py` stores a disposable preferred address under
`~/.cache/zephyr_remote_openocd/preferred-addresses/`. The filename is a SHA-256
digest of an unambiguous encoding of the configured SSH argv prefix and host;
the file contains only the canonical loopback address. Remotes with identical
SSH prefixes and hosts share a cached preferred address, while distinct
identities use separate files. The cache directory is created with mode `0700`
and temporary files with mode `0600`. Closing the temporary file and atomically replacing the destination
prevents partial updates. No cache lock is needed: concurrent updates may select
either successfully forwarded address, and the helper independently validates
every subsequent preferred address.

The session reads a preferred address only when it selects initial or reserved
services. It updates the cache after each non-empty required forwarding batch
succeeds, including deferred RTT forwarding. Failed required startup and best-effort-only
forwarding do not update it; flash neither reads nor writes the preferred address
cache. Cache read, validation, and write failures are ignored. No cache durability,
retention, or successful-reuse guarantee is needed for correctness. Recording mode never
opens a session and does not access this cache.

Reusing the same remote address and ports can allow an external sharing master
to reuse an already retained forward. Reuse is not guaranteed: an active lease,
occupied remote port, changed endpoint, lost hint, or changed SSH identity can
prevent it. A stale external forward may then keep the requested local port
occupied or point at an old remote endpoint. The user must manage such retained
state through their SSH client; the preferred address cache does not cancel or
repair it.

---

## 33. Cross-Client SSH Forwarding Setup

The forwarding setup uses multiple SSH processes.

### 33.1 Current design: multiple SSH processes

One SSH connection controls the helper.

Additional SSH processes provide forwarding.

Advantages:

- simple;
- uses the required SSH forwarding behavior.

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

The local staging builder creates a POSIX tar archive from the planned file and
directory manifest. The helper accepts only regular files and directories;
symlinks, hard links, and special files are rejected. Before extraction, it
validates every member's normalized relative path, rejects duplicate paths and
file/descendant conflicts, and checks that resolved targets remain strictly
below the staging root. An invalid member rejects the archive before any
member is extracted.

Extraction creates implicit parent directories as needed beneath the private
staging hierarchy, using normal OS permission and umask handling. Explicit
directory entries are forced to be owner-private, writable, and searchable.
File modes retain only owner permission bits, with an owner-readable/writable
fallback. Archive ownership and timestamps are not applied. The exact path,
member-type, and permission rules are defined in [protocol.md](protocol.md),
which is the sole staging wire-contract definition.

Before accepting staging or starting OpenOCD, the local session coordinator
requires a successful staging invocation and validates its sole `STAGED`
response. Both ordered file/directory manifests, the regular-file byte count,
and the SHA-256 content digest must match the locally built archive. A malformed
or mismatched confirmation fails session startup and invokes session cleanup.
The archive represents directory entries explicitly, including empty search
roots and empty nested directories, so staged OpenOCD search trees preserve
their required lookup structure.

---

## 35. Remote Helper Deployment

The helper is automatically deployed to a per-user location such as:

```text
~/.local/libexec/zephyr_remote_openocd/
```

Deployment also uses the configured SSH command.

The deployment computes the SHA-256 digest of the helper source and identifies
the revision with a path of the form
`protocol_v2/helper-<sha256>.py` beneath the per-user deployment directory.
It acquires an exclusive `fcntl` lock on
`protocol_v2/.deploy.lock` before checking, installing, refreshing, or
reclaiming revisions. An existing target is reused only when its content
digest matches. Otherwise, deployment writes the helper to a mode-0600
temporary file, flushes and synchronizes it, and atomically renames it to the
digest-named target. The selected target's timestamp is refreshed while the
lock is held.

Revisions matching `helper-*.py` that are older than 24 hours are eligible for
opportunistic reclamation, except for the selected target. Deployment attempts
to remove every eligible stale revision, but tolerates an individual stat or
removal failure and continues. Reclamation is best effort and never makes
deployment fail; the selected target is never removed. The deployment lock is
released before the session helper starts. Exact deployment response fields
remain part of the wire contract defined in [protocol.md](protocol.md).

The v2 namespace is selected at cutover; production still uses `protocol_v1`.
Client and helper move together, without a dual-protocol runtime or wrapper
fallback. The YAML schema, configured argv, staging archive, and path-template
contracts remain unchanged. A version bump records an incompatible wire change,
not a development milestone.

No assumption is made that the local SSH executable comes from the local Linux distribution.

---

## 36. Remote Helper Protocol

The selected internal helper wire format and behavior are specified solely in
[protocol.md](protocol.md). The SAD records the architectural consequences of
that contract; it does not duplicate message fields, framing, state
transitions, ordering, validation, or frame-size limits. The deployed client
and helper must implement one strict matching contract at cutover.

## 37. Remote Session Storage

Preferred:

```text
$XDG_RUNTIME_DIR/zephyr_remote_openocd/<session-id>/
```

Fallback:

```text
~/.cache/zephyr_remote_openocd/sessions/<session-id>/
```

Session workspaces and their storage root use owner-only directory
permissions. Staging directories and associated session metadata remain
beneath that protected hierarchy, so ordinary remote users cannot access
session files.

For opportunistic reclamation, a session workspace older than 24 hours is
eligible only when the helper can establish that it is not owned by an active
session and is not protected by an active staging operation. Orphaned session
coordination metadata older than 24 hours is eligible when its corresponding
workspace is absent and the helper can establish the same safety conditions.

When allocating a new session, the helper attempts to reclaim each workspace or
metadata entry that it can safely establish as eligible. If eligibility cannot
be safely established, or if inspection or removal fails, the helper may leave
the entry for a later allocation and does not repeat that reclamation attempt
during the same allocation. Normal cleanup failures remain visible under the
SRS cleanup requirements. This opportunistic policy does not guarantee bounded
disk growth when no later allocation occurs or when inspection or removal
continues to fail.

---

## 38. Process Supervision

The local operation decides dependent launch eligibility (§39); the remote
helper decides remote phase transitions. Forwarding and RTT retain their own
physical cleanup and failure domains. No cross-host global supervisor is added.
The detailed normal local-session sequence is defined in §39. The runner owns
the local `RemoteSession` lifetime and closes it when the operation finishes.
The helper may also end the remote session after OpenOCD exit, protocol failure,
or control-channel loss. The ownership boundaries are:

| Owner | Resources and decisions |
| --- | --- |
| `RemoteSession` | Coordinates staging and subsystem cleanup and reports session/cleanup failures to the active operation. |
| `_HelperClient` | Owns the helper control channel, protocol reader, helper observations, output delivery, and helper shutdown. |
| `_ForwardManager` | Owns local forwarding SSH processes, forward status checks, and forward cleanup. |
| `ManagedSshProcess` | Owns one local SSH subprocess and its stderr drain. |
| External SSH connection-sharing mechanism | Owns its master and any forwarding state retained independently of runner-launched subprocesses; outside runner cleanup. |
| Remote lifecycle authority | Owns remote phase transitions, attempt admission, outcome selection, workspace lifetime, and cleanup coordination. |
| Child physical owner | Owns the acquired OpenOCD process group, output relays, termination, reaping, and stream closure; reports facts to the remote authority. |

`RemoteSession.close()` coordinates the subsystem cleanup sequences and chooses
the primary failure across helper and forwarding cleanup. Failure of one
subsystem cleanup does not prevent the other applicable cleanup sequence from
being attempted. Each owner cleans up the resources it acquired; no owner
transfers an active resource to another owner merely because cleanup encountered
an error.

`RemoteSession.close()` blocks SIGINT before entering forwarding or helper
cleanup and keeps it blocked through every applicable bounded cleanup attempt
and marking the session closed. It then restores the caller's exact previous
signal mask. Subsystem entry and the transitions between cleanup owners are
protected along with each owner's cleanup sequence.
Any interruption delivered by mask restoration is reported after these
attempts; an earlier cleanup failure remains primary and retains the
interruption as a diagnostic.

### 38.1 Local SSH subprocess ownership

`SshCommand.popen()` starts each long-lived control or forwarding transport with
SIGINT blocked, while preserving the caller's controlling terminal and
foreground process group for interactive SSH authentication. The launch thread
temporarily blocks SIGINT so the child inherits that mask across exec, then
establishes managed ownership and starts the stderr drain before restoring its
exact previous mask. The drain inherits blocked SIGINT and stops through pipe EOF
and explicit cleanup. Existing threads' masks and process-wide signal handlers
remain unchanged. Keeping the transports in the foreground group preserves
terminal authentication without introducing background-group SIGTTIN reads.

The inherited mask is a launch-time mitigation, not unconditional SIGINT
isolation. Terminal SIGINT intended for GDB leaves a transport unaffected
while its client retains SIGINT blocking. The executed client or wrapper can
change its mask, including unblocking a pending SIGINT, and may then terminate.
SRS §2.8 does not require clients to preserve the inherited mask, and this
behavior is not an additional SSH compatibility requirement. The terminal
SIGINT integration test exercises a synthetic transport that resets the signal
disposition but retains the blocked mask; it does not establish survival for
all compatible SSH clients.

If a client terminates on SIGINT, resulting control-transport or required-forward
loss follows the existing session-failure and bounded-cleanup rules. Loss of a
best-effort forward follows the existing warning policy. The runner does not
transparently reconstruct an interrupted debugging session.
Explicit lifecycle cleanup still terminates and reaps each owned SSH process
directly. If restoring the launch thread's mask delivers a pending interruption,
the transport boundary rolls back the managed process before propagating it. The
ownership transition and return remain inside the rollback guard, including the
interval after mask restoration.

`ManagedSshProcess` remains a narrow ownership wrapper rather than a session
abstraction. It delegates process status and termination to the underlying
SSH subprocess and owns exactly one `_StderrDrain`. Standard input and output
remain available to the session protocol or forwarding-startup owner, while
the stderr pipe is detached from the subprocess object and transferred to the
drain so that it has only one local owner.

The current design uses a per-process drain thread because a long-lived SSH
client may emit more diagnostic data than an operating-system pipe can hold
while its stdout still carries protocol or readiness data. The drain retains
only a bounded byte tail. A lock protects that tail because failure observation
may read it while the drain thread is still appending data.

Shutdown waits for drain completion and thread exit within one shared bounded
budget before closing the stderr stream. Closing a buffered pipe while another
thread is blocked in `read()` can itself block on the stream's internal lock.
If the SSH process or a descendant still holds the write side and EOF does not
arrive, cleanup therefore reports failure and retains the stream rather than
turning stream cleanup into an unbounded wait. Close serialization and an
explicit closed flag keep repeated cleanup attempts harmless. The drain thread
is a daemon so an uncooperative inherited writer cannot hold local process
shutdown open indefinitely.

This separation is intentional: `_HelperClient` and `_ForwardManager`
perform process cleanup within their own subsystem sequences,
`RemoteSession.close()` coordinates those cleanup sequences, `ManagedSshProcess`
exposes per-process control and diagnostic access, and `_StderrDrain` alone
owns stderr consumption and stream closing. In this design, removing the
wrapper, drain thread, bounded tail, or bounded reader shutdown would require
another mechanism that preserves those ownership, backpressure, diagnostic,
and bounded-shutdown properties; simply removing them would introduce dual
ownership, permit pipe backpressure to stall the session, lose actionable SSH
diagnostics, or make cleanup potentially unbounded. The per-process wrapper
does not replace the helper and forwarding resource owners.

`RemoteSession` is the sole local remote-session coordinator and owns all
runner-acquired local session resources, excluding externally retained sharing
state as described in §32. `RemoteSession.prepare()` deploys the helper revision
and returns a coordinator with no live session resources. The adapter adopts
that coordinator before calling its instance `acquire()` operation, and
releases it through cleanup-only `RemoteSession.close()`. `RemoteSession.open()`
is a convenience for callers that need a usable returned session; it combines
preparation and acquisition with startup rollback. The helper client and
forward manager own their respective resources and cleanup sequences beneath
this boundary. The session stores a resource-free `_HelperClient` before calling
its instance `acquire()`. The client briefly blocks SIGINT while launching and
adopting the control transport, then restores the caller's mask before awaiting
`SESSION_CREATED`. Thus pending launch interruption is delivered with transport
ownership established, and helper startup remains interruptible. Protocol-reader
startup likewise blocks SIGINT until the started thread is registered with the
client, then restores the caller's previous mask. Pending interruption therefore
leaves that reader owned for shutdown instead of permitting a second reader.
If acquisition
ends before the allocation is accepted, client cleanup terminates and reaps the
owned control process while closing its input lease and reading any available
final outcome. An owned client with no acquired transport closes harmlessly. Helper acquisition and
startup rollback belong to the session owner; `_HelperClient` has no
resource-acquiring factory. A session is single-use: it cannot be reopened or
restarted.

OpenOCD launched as the session process of a remote-runner session executes
in a helper-supervised process group and session. The process group is the
helper's ownership boundary for generic cleanup hygiene, including processes
that outlive the OpenOCD leader. This process-group supervision contract does
not apply to standalone helper operations.

In particular, `openocd-version` is a standalone helper operation subject to its
SSH invocation timeout. That timeout bounds the local SSH command invocation;
it does not provide the session's process-group supervision or
descendant cleanup guarantee.

SSH loss has independent local and remote observations. Local detection is
delegated to the configured SSH client and local operating system; remote
detection is delegated to the remote SSH service, operating system, and the
helper's control-channel observation. The runner does not impose a bound on
either detection latency or on the interval between the two observations. A
local SSH failure does not prove that the remote helper has begun cleaning up:

```text
underlying connection becomes unusable
        |
        +--> local SSH client/OS detects loss
        |          |
        |          +--> local operation fails
        |               bounded local cleanup attempt
        |
        +--> remote SSH service/OS delivers EOF or signal
                   |
                   +--> helper observes control loss
                        bounded OpenOCD process-group cleanup
```

Only the cleanup attempt on each side is bounded, beginning after that side
observes loss. The project does not bound the interval from local detection to
remote OpenOCD termination.

### 38.2 Remote lifecycle authority

The Phase 2 foundation in
[`remote/lifecycle.py`](../../python/zephyr_remote_openocd/remote/lifecycle.py)
implements a pure transition authority with immutable phase and attempt records.
Its read-only request role keeps the validated concrete request available to the
coordinator without coupling the model to Zephyr or physical owners. Attempt
entry and READY consume admission facts; child observation records the first
genuine status and the preceding cleanup-signal context. Retry consumes safe
classification and separate producer/resource settlement facts. Termination
prevents further entry or activation, including after a late acquisition result.
Closure freezes one snapshot; later failures remain local diagnostics.

This authority is not yet wired into the deployed helper. Protocol v1's current
authority and fences remain the only remote runtime authority until adaptation
and cutover. Physical adapters must validate eligibility before wire admission,
retain immediate acquisition ownership, and establish the facts passed into the
foundation. Boolean admission/liveness/settlement facts are not physical effects
or proof of adapter correctness. No new effect queue or controller-recognition
fence is introduced.

[`remote/outcome.py`](../../python/zephyr_remote_openocd/remote/outcome.py)
provides immutable diagnostics, child results, outcomes, disposal reports, and
terminal snapshots. Existing exception notes can be captured at a boundary as
nested diagnostic values without mutating the source exception. The first
failure stays primary and subsequent failures retain order and nested detail.
Independent helper/SSH validation and bounded wire encoding remain cutover work.
Flash and debug plans now select completion policy explicitly in the internal
process description; Protocol v1 START serialization is unchanged.

One remote coordinator decides lifecycle transitions. Control-input, stream,
child-exit, deadline, writer, and signal adapters report facts; none independently
chooses shutdown, success, or retry. A standard-library asyncio structured task
scope owns those observers and the writer. Its outer rollback scope retains
workspace, signal-handler, and descriptor restoration responsibility during
partial setup and after observer shutdown.

The control entry point observes termination signals before workspace allocation.
Allocation owns the workspace, lease metadata, and session lock until explicit
coordinator adoption. Construction or event-loop setup failure attempts workspace
rollback and lock release independently, preserving the setup failure. A latched
signal remains observable across this transfer. No child exists yet.

Lifecycle data is phase-specific, conceptually:

```text
Created(workspace owner)
Starting(immutable request, current attempt, readiness evidence,
         provisional retry failure)
Active(owned child attempt)
Terminating(attempt or residual owners, outcome)
Closed(outcome, residual obligations, frozen terminal snapshot)
```

These are design roles, not required class names. Phase-specific data prevents
invalid combinations such as active readiness without child ownership. Parser
bytes, decoder buffers, writer offsets, native signal latches, descriptors,
acquisition tickets, and cleanup-task completion are physical adapter state;
they do not provide a second transition authority. `Closed` means the terminal
snapshot is frozen, not that every physical disposal necessarily succeeded.

The immutable request carries an explicit `completion_policy` selected by the
Zephyr adapter: `live_server` for debug/attach/debugserver/RTT, `process_exit` for
flash. Neither markers nor services select the policy. Starting covers process
acquisition for both; an owned `process_exit` attempt becomes Active without
READY or a readiness timer, while `live_server` remains Starting until READY
admission. One-shot child exit drives its result even if it arrives before
normal adoption completes; acquisition ownership and settlement still apply.

An attempt progresses through authorization, production, ownership, and
settlement. Each authorized replacement has a new monotonically increasing
session-local generation. Facts identify their attempt; an obsolete generation
cannot adopt current ownership, satisfy readiness, establish current result, or
initiate retry. Rejecting a stale returned resource still invokes its physical
cleanup owner; rejection is not disposal.

At actual attempt entry, the authority validates phase and generation, admits
an immutable exact-argv ATTEMPT diagnostic, establishes acquisition ownership,
and invokes spawn in the same effect boundary. There is no asynchronous effect
command queue between that validation and spawn. Failed diagnostic admission
prevents spawn. Admission means bounded local writer acceptance, not peer receipt.
Native signal-safe acquisition remains necessary even in a section with no
`await`.

### 38.3 Retry and readiness decisions

Only a `live_server` child exiting before readiness with a classified safely
repeatable bind/address failure is eligible for retry. A `process_exit`
child's exit supplies the one-shot result and does not initiate child retry;
pre-spawn address-candidate selection remains available for both policies.
Preserve the current bounded startup-output classifier for the
case-insensitive phrase `address already in use`; it is not a structured
OpenOCD code or universal bind classifier. Other startup failures, probe
contention, and exit after readiness do not retry. At most 32 child attempts,
including the initial attempt, are permitted.

Before replacement, the prior acquisition producer must actually finish and
its process-group, observer, stream, and address ownership must settle enough
for safe reuse. A cancelled task or expired wait is not proof of producer
quiescence. Release the old address lease before allocating and validating a
fresh randomized address (§27). Cleanup failure prevents retry. A classified
startup failure stays provisional while this safe retry remains possible. If
retry is exhausted or settlement fails, startup fails; controller termination
may instead initiate closure, retaining the old failure as attempt diagnostic.
An established failure is never made provisional again. Restart does not
roll back target effects or certify arbitrary user Tcl as idempotent.

The authority revalidates Starting state, generation, settlement, and absence
of its own terminal decision at entry. It may account an immediately available
controller-termination fact first, but no controller-reader checkpoint, ACK,
publication fence, or recognized-prefix drain is required before retry or READY.
EOF occurring, an adapter recognizing it, publishing it, and the authority
handling it are distinct stages. READY or retry can precede authority handling;
after termination commits, neither can be newly authorized.

This intentionally removes Protocol v1's recognition-level success precedence.
The removed ordering had no remaining product requirement: local cancellation
already revokes dependent launch, and remote handling of controller loss cleans
all owned work. Fatal physical observer/writer errors still become failures,
and established primary failure is preserved; their reports must not be lost
merely because a task is cancelled before queue publication. Already recorded
fatal physical failures are accounted before READY, retry entry, or terminal
publication, even if ordinary dispatch is pending. This local failure check
requires neither future controller input nor recognized-EOF precedence.
Physical ownership, producer settlement, and stale-generation validation remain
mandatory.

READY admission requires `live_server` policy, Starting state, current
generation, owned child, required evidence, and a final live-child check.
Failed admission causes termination rather than activation. Local receipt
independently feeds the local launch gate. The bounded final readiness
observation policy is specified in §40.

### 38.4 Child physical ownership

An acquisition ticket or rollback scope exists before each resource-producing
effect. It owns the effect's returned resource immediately, through construction
and validation, until an explicit transfer to the longer-lived owner. Transfer
updates cleanup reachability inside the same interruption-safe guard; rollback
must remain possible immediately before and after transfer. Resource production
is settled by actual producer completion and disposition of any returned resource,
not by cancelling its awaiter.

| Resource acquisition | Immediate / partial owner | Transfer and rollback |
| --- | --- | --- |
| Local SSH Popen | Configured transport launch guard | Managed process/drain adoption inside the guard; launch or mask-restoration failure terminates/reaps and closes owned streams. |
| Workspace, lease metadata, session lock | Allocation rollback scope, including partially created artifacts | Remote coordinator adoption transfers all artifacts; setup failure attempts workspace and lock cleanup independently. |
| Remote address lease | Current attempt acquisition scope | Transfer to child attempt only with continuous reachability; failed materialization/spawn releases it, retry releases old lease after child settlement. |
| Child Popen and its pipes | Native interruption-safe acquisition ticket | Child physical owner adopts inside rollback guard; interruption on either side disposes the group, reaps, and settles pipes. |
| Observer tasks and protocol writer | Task-creation/setup scope | Register with session/attempt owner before publication; setup failure joins/cancels under bounded policy and retains unresolved obligations. |
| Staging shared lease | Standalone staging scope | Held through validation, extraction, and success reporting; always released on failure/process exit, with no transfer to the controller. |
| Forwarding transport | Forward manager's pending-resource owner | Keep rollback ownership through validation/adoption; independent failed forwards cannot orphan an acquired process or roll back another active forward. |
| RTT socket and terminal mode | RTT connection/terminal scopes | Socket remains owned through connect/adoption; terminal restoration and socket closure are independently attempted on failure/interruption. |

A `SupervisedChild` owns the configured OpenOCD process-group resources and
per-stream decoding state, which only the coordinator consumes. Process
creation retains `Popen(start_new_session=True)` and explicit reaping: using
asyncio's subprocess transport would automatically reap the leader before the
owned-group cleanup decision. The leader is observed non-destructively with
`waitid(..., WNOWAIT)`, using pidfd readiness where supported and an async
bounded-interval observation fallback on older Linux kernels. An unreaped
leader protects the process-group identity until group signalling completes.
The helper allocates and leases the remote loopback address and checks initial
and reserved service ports for bind collisions before startup; OpenOCD owns and
configures the actual GDB, Tcl, telnet, and RTT listeners. The helper does not
probe listener connectability.

The input adapter owns one raw reader and bounded incremental START framer.
After valid START, it observes EOF or unexpected input while startup and active
work proceed. Each child stream likewise has one raw reader and incremental
decoder. Normal decoding and the final startup scan share that ownership;
there are no competing readiness readers. Their facts reach the authority
independently of bulk-output delivery.

Phase 3 extends the owned raw reader's checkpoint from one read to a finite
available-byte scan. The same reader consumes until EOF, a would-block result,
or a one-MiB byte budget; continuous arrivals cannot prolong determination
indefinitely. This budget matches the maximum control frame and is an internal
observation bound, not a timeout or a new product setting. Decoding and ordered
publication remain on the normal observation path. The coordinator continues
dispatching while checkpoint facts are published, then performs its existing
child-status and retained-evidence decision. Reader cancellation or close still
settles the checkpoint. The Protocol v1 control fence remains in place.

### 38.5 Output and outcome ownership

One nonblocking ordered writer retains a maximum of 16 MiB of encoded pending
output. Lifecycle-critical observations have an independent reporting path and
cannot wait behind a full bulk-output queue. The selected policy does not reserve
separate writer capacity for critical frames: exceeding the bound, including
failed ATTEMPT or READY admission, is a visible infrastructure failure that
initiates cleanup. The product does not promise terminal delivery under a
congested or failed output path, so reservation would not establish another
required guarantee. This choice preserves bounded memory and cleanup progress;
it must be qualified with actual blocked-pipe and capacity-failure regressions.

Phase 3 retains that writer topology and capacity. Reader and writer descriptor
mode acquisition now rolls back an effect that succeeds before constructor
adoption fails, preserving the initiating exception and rollback detail. Writer
close releases retained frames independently of mode-restoration failure. The
signal scope owns both installed handlers and a plain first-signal latch;
handlers report facts without deciding transitions or mutating asyncio queues.
Signal-handler, descriptor, workspace, and allocation rollback cleanup continues
across `BaseException` failures while preserving the primary outcome and nested
secondary notes on the current Protocol v1 boundary. Failed retirement of an
address lease prevents a replacement spawn and leaves its cleanup owner reachable.
Address allocation likewise retains its reservation until all temporary TCP
probes have closed and the return value can adopt it. A probe finalizer or return
construction failure attempts every independent socket release and prevents
candidate retry when cleanup remains unsuccessful. Ordinary pre-spawn candidate
selection remains distinct from a post-spawn child retry.

Bounded accumulation also applies before writer admission: raw-observation queues,
per-stream decoding/marker capture, and structured diagnostic retention cannot
hide an unbounded buffer behind the encoded-output limit. Overflow reports
infrastructure failure while critical fact reporting and cleanup remain usable.

After resource cleanup, retained output has a two-second drain opportunity.
Writer failure or drain expiry cannot undo disposal. Best-effort descendant
warnings use nonblocking stderr and may be dropped under backpressure.

The canonical outcome is structured data: initiating trigger, primary failure,
ordered diagnostics with nested detail, and genuine child result. Disposal and
residual obligations are recorded separately. Once established, a primary failure
cannot be replaced by later cleanup, transport, writer, or observer failure.
Unrelated unestablished failures need no globally deterministic winner.
Exception notes may be rendered at a boundary; exception structure is not the
canonical result. Existing notes on secondary exceptions must survive conversion
into structured diagnostics.

The authority freezes at most one terminal snapshot after cleanup attempts and
retained-output accounting. Admission or partial-write failure cannot create a
second snapshot. Failures discovered after freezing extend local diagnostics
only. Child status is recorded from actual child observation, including during
shutdown; controller EOF does not imply a fabricated code. Retired-attempt
results stay attempt diagnostics and cannot replace the final attempt's result.
Helper/SSH status is always separate infrastructure evidence. Child-result
provenance also records whether the helper had requested child termination
before observing the status. Before sending a cleanup signal, account any
already observable natural exit. A disposal-induced nonzero status after
successful GDB/RTT work is retained as a result without turning ordinary server
shutdown into operation failure. A natural failure already established during
operation remains primary. This observation boundary does not establish a global
natural-exit versus EOF winner.

Cleanup sends `SIGTERM` to the owned group and waits a bounded grace period for
the leader. It then checks whether the group still exists. If so, the helper
may inspect `/proc` once and warn about observable non-leader members before
sending `SIGKILL`. Failure or a race during this best-effort diagnostic does
not affect the group cleanup decision or success criterion; complete `/proc`
enumeration is not required.

Phase 3 adapts the current standalone helper's physical boundaries while its
Protocol v1 coordinator remains the runtime authority. A generation-scoped
acquisition ticket is reachable before `Popen`, retains the raw process through
supervisor construction, and retains the supervisor through coordinator
adoption. Both construction rollback and an interrupted return use this same
cleanup reachability; partially adopted streams still receive owned observers.
Native handlers latch facts throughout those synchronous effects.

Physical settlement records producer return, verified group disappearance,
joined observers, and closed pipes separately. Finishing a cleanup coroutine
does not confirm disposal. Retry requires all those facts as well as the
existing classification and Protocol v1 control fence. Before cleanup signalling,
the process owner observes genuine leader status without reaping its identity;
delivered termination is recorded independently from that result. Group signals,
finite escalation, reaping, and disappearance checks retain their existing OS
mechanisms, with independent cleanup attempts after interruption or failure.

Terminal cleanup publishes staging closure before child settlement. Admitted
uploads retain their shared lease, so process cleanup can progress independently
and removal still waits for exclusive workspace access. A conservative
`.child-disposal-unconfirmed` marker is published before spawn. Verified child
settlement retires the marker before normal workspace removal; otherwise the inputs and marker
remain, new staging is rejected, independent local descriptors are closed, and
cleanup fails. Stale reclamation skips marked workspaces even after helper exit
or the age threshold: neither proves residual child disposal. Removal of such
retained data requires independently established disposal.
Once child disposal is confirmed, a staging or filesystem removal failure alone
does not prevent later stale reclamation under the existing exclusion mechanism.

The helper then attempts to reap the leader with a finite budget. After that
attempt, it polls group existence with `killpg(pgid, 0)` under a separate
one-second deadline. Only `ESRCH` (Python's `ProcessLookupError`) confirms that
the group is gone. Successful signal delivery or leader reaping alone does not
confirm complete group termination. A still-observable group at the deadline,
or another error from the existence check, is a cleanup failure. Expiration
remains a cleanup failure even if the group disappears later. A failed
signalling or reaping operation remains a cleanup failure even if the final
check finds no group. Reaping precedes the final check because an unreaped
leader itself can keep the group observable; zombie descendants likewise keep
it observable until their adopter reaps them. No further termination signals
are sent after the reaping attempt, so a reused numeric process-group ID is
never signalled during final confirmation. Startup ownership rollback uses the
same final bounded existence criterion after its kill and reaping attempts.

Regardless of process-group cleanup success, the helper drains output
observations with a shared bounded deadline, and cancels and awaits remaining
attempt tasks before releasing their streams. Graceful leader waiting and final
group observation waits are async, so output can continue draining throughout
supervised termination. The session cancels and awaits all observer tasks before
its TaskGroup ends.
Workspace cleanup closes new staging admission when termination begins,
before waiting for child disposal. Independent process/relay cleanup proceeds
without first blocking on the staging lock. Remove the workspace only after
acquisition producers have settled, child disposal is confirmed, and exclusive
staging exclusion is obtained. Unconfirmed child disposal retains required
inputs, the lease/closure metadata, and explicit residual responsibility.
A session lock may be released on helper exit, so later reclamation must also
establish that retained inputs have no dependent child/producer; helper-lock
availability alone cannot prove that a failed cleanup left a workspace safe.
The implementation of that residual evidence belongs to physical-boundary work,
not to a new lifecycle authority or experimental subreaper implementation.

### 38.6 Signals and cleanup composition

Unix signal callbacks latch a fact and wake the authority. They do not select a
terminal cause, independently change phase, or dispose resources. A latch before
loop setup is reported once the loop exists. Native masking or rollback scopes
protect acquisition and handoff separately from coordinator transitions; lack
of an `await` does not make a Python section atomic against signal handlers.
After the authority commits termination, subsequent signals cannot abandon
independent cleanup actions. Signal restoration attempts each prior handler
even when another restoration fails.

When the authority accounts native SIGINT/SIGTERM delivered to the helper,
it records helper/session failure for both completion policies. A signal that
initiates shutdown selects the signal trigger and establishes primary failure;
an existing primary is preserved with signal detail secondary. A signal during
EOF-initiated cleanup still records failure without replacing the trigger or
interrupting independent disposal. Confirmed cleanup and genuine child status
zero do not make this failure successful. If the wire snapshot is already frozen,
the signal remains a local diagnostic and forces nonzero helper status rather
than changing or replaying that snapshot. Client helper-status validation retains
that late failure independently of the received outcome.

Intentional local shutdown closes the controller input lease. The helper's
SIGTERM/SIGKILL of its owned child group is ordinary child disposal, not a native
signal delivered to the helper. Interactive local GDB Ctrl-C retains its separate
stock behavior; it does not justify treating remote helper signals as success.

Cleanup composition preserves the established primary and nested secondary
diagnostics. A failed owner cannot skip another independent owner's attempt.
An expired task wait leaves residual ownership or a supervised obligation;
it never marks its producer settled. The structured task scope cannot simply
discard a late acquisition result on cancellation. The final snapshot reports
unconfirmed disposal when physical settlement cannot be established.

### 38.7 Workspace and staging exclusion

Each session holds an advisory lock in its workspace. During new-session
allocation, the helper takes one directory snapshot of the session root and
checks each older workspace entry once. A missing session root is treated as
empty. An older workspace is eligible when the helper can inspect it, acquire
its session lock exclusively without blocking, and confirm that staging does
not protect it and no unresolved child or producer depends on its inputs.
A held lock means that the workspace is active and is skipped;
a missing or non-file session lock represents incomplete lock creation and is
handled as incomplete allocation only when dependent ownership can also be
excluded; uncertain entries are retained. The helper uses the closure and
lease-aware
workspace-removal procedure for each safely eligible workspace and does not
retry it during the same allocation.

Inspection, lock acquisition, or workspace removal errors leave that workspace
for a later allocation and do not stop inspection of other entries. Errors
while taking the root snapshot other than a missing root are propagated. Root
entries that are neither workspaces nor recognized staging metadata are
ignored.

Standalone staging first spools its upload without workspace ownership. Before
archive validation or extraction, it acquires a shared workspace lease using a
nonblocking lock, then checks that admission remains open and the workspace
exists. Allocation creates each sibling `.<session-id>.lease` before exposing
the workspace. Staging opens only that existing lease and never creates
coordination metadata. Cleanup atomically creates a separate sibling
`.<session-id>.closed` marker before attempting exclusive lease ownership. This
closure needs no mutex and remains observable even when another process is
suspended holding the lease. No root-wide admission lock is used.

If staging checks admission before closure, its shared lease protects the
workspace until extraction finishes and successful staging completion is
reported. If closure is
already visible, staging rejects even when it opened the lease file earlier.
After dependent child/producer disposal is confirmed, cleanup waits up to five
seconds for exclusive ownership before removing the workspace; a timeout reports
failure and leaves admission closed. A contended
lease affects only its own session. The existing `.session.lock` continues to
track control-helper liveness for stale reclamation.

After workspace removal succeeds, cleanup attempts removal of both the lease
and closure metadata while holding exclusive lease ownership. Each removal is
attempted even if the other fails; any removal failure is a session cleanup
failure. Successful normal cleanup leaves no per-session artifacts. If workspace
removal fails or times out, both coordination files remain to preserve closed
admission and the lease identity.

Orphaned metadata left by unsuccessful cleanup is eligible for opportunistic
reclamation once older than 24 hours. The same root snapshot considers sibling
files whose names identify `.lease` or `.closed` metadata. It attempts removal
of each eligible file once only when its corresponding workspace is absent;
metadata with an existing workspace is retained. Metadata inspection or removal
errors leave the file for a later allocation. A delayed stage either cannot
open the removed lease or checks workspace absence after locking an already
open descriptor; neither path can recreate session artifacts. Stale workspace
removal uses the same closure and lease procedure.
Kernel locks release on stage-process exit, including uncatchable termination.
All lease acquisitions are nonblocking; cleanup retries have a bounded
deadline. Filesystem operations retain their ordinary OS behavior.
Standalone staging behavior and protocol framing remain coordinated through
[protocol.md](protocol.md).

---

## 39. Local Session Lifecycle

The Phase 2 foundation in
[`remote/launch.py`](../../python/zephyr_remote_openocd/remote/launch.py) models
local launch eligibility without integration into the production session.
It requires observed readiness and phase-specific forwarding, preserves
cancellation and fatal failure across boundaries, and rejects stale queued
generations. Its pure tests establish these transition semantics.

Integration awaits the later client/session adaptation. The Protocol v1 helper
reader can record a fatal fact after a session check returns but before gate
entry. Checking the helper and then entering an independent gate does not
account for that already-recorded fact at actual execution entry. The adapted
coordinator must give fatal observations and launch eligibility a shared
authority boundary before this gate can protect production launch. A test that
synchronously closes the session during observation only tests direct gate
cancellation; it does not qualify that concurrent reader boundary.

Current GDB/RTT entry retains its existing Protocol v1 session checks and uses
Zephyr's `run_client`, including native interactive SIGINT handling. Cleanup
ownership, the SIGINT-masked cleanup scope, and the STOP path remain unchanged.
The following local lifecycle describes the redesign target.

The local operation alone owns dependent launch eligibility. Its conceptual
phases are Opening, Active, Cancelling, and Ended. These roles apply to each
dependent launch boundary, including GDB setup followed by an RTT client; an
RTT phase transition does not reopen a cancelled operation.

```text
prepare and adopt resource-free coordinator
    -> acquire helper transport and session workspace
    -> stage inputs; send immutable START
    -> keep helper stdin open as controller lease
    -> observe ATTEMPT diagnostics and remote READY
    -> establish required forwarding
    -> validate local launch eligibility at actual entry
    -> run GDB / RTT / foreground server / one-shot result wait
    -> commit local closure; close helper stdin
    -> read final output/outcome and settle owned local transports
```

The adapter owns the prepared coordinator before acquiring live resources and
finalizes it from the same exception scope on success, failure, or interruption.
The coordinator in turn owns helper and forwarding owners before acquisition.
Existing pending-resource rollback for forwarding and RTT is retained. No
live resource is handed through an unguarded return interval.

For a dependent launch the gate requires the local operation to remain Opening,
remote READY to have been received for the current attempt, required forwarding
to be established for this phase, and no committed cancellation or established
fatal failure. Recorded terminal/fatal facts are accounted before publication
of local usability or launch, including facts recorded during forwarding. Even
natural child status zero is premature termination at a live-client boundary.
A queued launch revalidates at actual execution entry; authorization before an
asynchronous scheduling boundary is insufficient. A late READY after local
cancellation cannot reactivate the gate. This checks known facts, without
promising continued process liveness after the check.

Flash explicitly selects `process_exit` and waits for its genuine one-shot
child result and infrastructure outcome; no READY is admitted or expected.
Debugserver establishes its required forwarding and supervises the foreground
server without a GDB launch. RTT keeps its existing setup/client phases,
configured port, separate socket and terminal ownership, and
required/best-effort forwarding transition. GDB Ctrl-C retains stock
interactive behavior rather than automatically committing local cancellation.

Local closure first commits cancellation where applicable and prevents further
launch. It closes helper stdin once, retaining stdout/stderr readers for final
output and SESSION_ENDED. A shared 15-second coordination budget covers waiting
for terminal information and ordinary helper-transport exit. On expiry or
transport failure, cleanup escalates only the owned local SSH process with finite
termination/reaping budgets and closes its owned pipes/readers/drains. Forwarding
and RTT cleanup are independently attempted even if helper coordination fails.
This is bounded project-controlled effort, not a promise about OS blocking or
remote detection latency.

The local owner records remote disposal confirmation, child result, and local
helper/SSH status independently. Missing terminal information, including with
zero SSH status, is infrastructure uncertainty and cannot establish remote
cleanup success. A nonzero helper status after a valid remote snapshot remains
an infrastructure failure without changing its child result. Local timeout may
leave remote cleanup unconfirmed even after local SSH is reaped. External
sharing masters are never terminated by session closure.

Repeated closure is harmless; cleanup errors do not require automatic retry.
The established active-operation failure remains primary, with later shutdown
and child-result diagnostics retained. During an interactive local client,
fatal session facts are recorded and acted upon at the next defined status check
after that client returns; asynchronous interruption of GDB is not required.

---

## 40. OpenOCD Startup Readiness

The runner starts a dependent local client only after OpenOCD startup
readiness is satisfied. Startup readiness is one fact; it does not prove that
every forwarded service can accept a connection.

The runner adds two OpenOCD startup output markers for `debug`, `attach`,
`debugserver`, and `rtt` operations. It places an init-complete echo in
OpenOCD's post-init command list
before board configuration files and appends a startup-complete echo after the
full server startup sequence. Within its generated arguments, the runner also
sets the remote bind address and service-port settings before those files.
These are runner-owned session and transport properties, so an `init` triggered
by those board configuration files sees the runner's transport settings.
Board configuration files own probe and
target setup. A board or user configuration that overrides the runner's bind
address or service ports is outside the supported compatibility boundary. The
runner does not statically inspect arbitrary Tcl for conflicting commands.

The configured `openocd_command` executable and fixed arguments precede all
runner-generated arguments as an opaque prefix. Fixed arguments are an
advanced escape hatch: their exact values and order are preserved, and their
OpenOCD/Tcl semantics are not parsed, classified, reordered, or validated.
Users own conflicts between fixed arguments and with runner-generated
arguments, and initialization or listener creation before runner-generated
bind/service configuration takes effect unless they intentionally accept it.
For example, fixed `-c init`, `-f early.cfg`, or nested Tcl may execute before
the runner's transport settings. Startup-ordering guarantees apply only to
runner-generated arguments and do not cover arbitrary prefix behavior.

The Zephyr adapter logs the full effective remote OpenOCD argv at the runner's
debug level, visible with `west -v`. The helper materializes the command once
per attempt, validates required paths, and reports that exact argv through the
protocol's pre-spawn process event immediately before spawning. The client
invokes the session's process-start observer while awaiting readiness; the
adapter shell-escapes the reported elements without reconstructing expansion.
This preserves the diagnostic on spawn, readiness, and required-forwarding
failures, and reports each bind-collision retry with its actual workspace and
allocated address. Only explicit runner-owned templates receive workspace or
address values; all other arguments remain literal, and templates cannot target
the configured prefix.

The pre-spawn event's wire ordering and full-contract compatibility
requirements are defined in [protocol.md](protocol.md). The architectural
retry eligibility and attempt policy are defined in §38; the protocol document
defines diagnostic admission and the intentional controller-termination races.
Version compatibility is supplied by content-addressed deployment, which
installs the matching helper revision automatically.

The runner's init-complete and startup-complete markers establish distinct
lifecycle facts. The helper applies the readiness rules defined in
[protocol.md](protocol.md) to those observations. The init marker covers
explicit `init`, config-triggered initialization, and OpenOCD's normal
automatic initialization when `--no-init` is used. If RTT server startup is
part of the sequence, successful `rtt server start` precedes the
startup-complete marker, so startup readiness follows that command causally.

The three relevant facts are distinct:

| Fact | What it proves |
| --- | --- |
| OpenOCD startup readiness | The configured startup output markers were observed and the helper can report readiness under the protocol contract. |
| SSH forward established | SSH accepted and started a local forward. |
| Service reachable | A client connected successfully through the forward to the remote listener. |

The helper allocates a session loopback address and checks runner-selected ports
for bind collisions. OpenOCD owns its enabled GDB, Tcl, telnet, and RTT
listeners. Startup readiness does not wait for remote service sockets to become
connectable. Tcl and telnet are compatibility endpoints, and their remote
socket connectability is not a startup condition. Their configured local
forwarding processes still start as independent best-effort attempts during
`RemoteSession.open()` when their runner port options are enabled.
Required-forward startup failure, failed startup rollback, or an already-recorded
fatal helper outcome prevents the session from opening; best-effort
unavailability with successful rollback produces a warning. The active RTT
client must connect to establish end-to-end reachability.

For `live_server` with no required markers, startup evidence is immediately
satisfied, but READY still requires current ownership, final live-child
validation, and writer admission. Under `process_exit`, markers do not gate
completion, no READY is admitted, and no readiness timer is started. The request's
marker/timeout fields remain validated but unused for readiness under that policy.
The nominal `live_server` OpenOCD readiness deadline remains 30 seconds.

Deadline expiry initiates a finite local determination, not automatic timeout.
First account retained decoder/marker evidence, then consume already-read but
undecoded bytes and perform one nonblocking scan of each owned child stream
within a fixed 64 KiB per-stream byte budget. Already-read bytes precede kernel
bytes in that stream and consume the same budget.
Account EOF if observed within that scan, flush decoder state for actual EOF,
and check current child exit without a blocking wait. Readers share their
stream owner so ordinary observation cannot race a competing final reader.
The budget is fixed at determination entry and is not replenished by new output;
a noisy producer cannot extend the observation indefinitely. This policy may
include bytes arriving during the scan and may exclude bytes beyond the finite
cut; it is not a global timestamp guarantee.

Complete marker evidence at that boundary can satisfy readiness, but a dead
child cannot authorize live READY. Missing evidence commits startup failure
unless the authority has already committed termination. EOF and the deadline
may race with either initiating cause. No controller-observer checkpoint or ACK
is part of the final scan. Observation still accounts fatal physical adapter
failures without imposing Protocol v1 recognized-prefix ordering on controller
EOF. Exact complete-line/EOF marker semantics belong in [protocol.md](protocol.md).

---

## 41. OpenOCD stdout/stderr

Remote OpenOCD output is relayed with bounded low buffering. The helper reads
each child stream in bounded chunks and emits output fragments through the
protocol contract. Long newline-free output becomes visible before the child
exits. Fragment order is preserved within each child stream. Exact output
representation, decoding and marker-recognition rules, cross-stream
serialization, delimiter handling, and terminal-event ordering are defined
solely in [protocol.md](protocol.md).

This includes:

- diagnostics;
- flash progress;
- GDB diagnostics;
- RTT diagnostics;
- semihosting console output.

Application console output is not interpreted as application-level data or
semantically rewritten. The helper does inspect output for configured complete
startup-marker lines and the bind-collision diagnostic when making readiness and
retry decisions. Relayed output is also represented through the protocol's
incremental UTF-8 decoding with replacement and fragment rules, so the payload
is not a byte-preserving channel.

---

## 42. Error Handling

The protocol reader records facts and wakes waiters. It does not close the
session, terminate forwarding, choose the primary failure, translate helper
status into an OpenOCD status, or decide what the runner reports.

The OpenOCD result is populated only by genuine child observation carried in
the structured terminal snapshot, independently of the shutdown trigger.
Nonblocking status checks and waits for terminal information preserve the
separation between result, remote disposal confirmation, and local cleanup.
The runner checks forwarding status at the existing points in the active local
operation and with bounded local polling while waiting. A best-effort failure
produces a warning at the next forwarding status check. Interactive GDB has no
concurrent forwarding watcher, so that warning may wait until the client call
returns. Background GDB supervision remains separate work.
Session-fatal helper, protocol, or control observations recorded while an
active local client is running are acted upon at the next session status check
after that client returns. The runner is not required to asynchronously
interrupt the local client solely because such an observation was recorded. A
failure that itself removes required transport may naturally cause the local
client to return earlier.

The first operation failure already established during the active operation
remains the primary failure. If no earlier failure exists, a helper, protocol,
SSH/control, required-service forwarding, or required cleanup failure becomes
the operation failure. Best-effort forwarding startup failure produces a warning
only after successful rollback, and best-effort forwarding exits produce
warnings.
Cleanup failures affecting acquired resources remain fatal regardless of whether the
service was required or best-effort. A later OpenOCD result or session failure
is retained as diagnostic information when it cannot replace the primary
failure. The helper converts retained secondary details, including existing
exception
notes, into structured diagnostics before freezing SESSION_ENDED. Rendering
exceptions at a caller boundary preserves that detail without making notes the
canonical lifecycle result.
Combining failures retains notes already attached to each secondary exception,
including diagnostics from nested cleanup batches. Later cleanup composition
preserves those details even when the secondary exception's summary was already
attached to the primary failure.
The following table applies to independently observed natural OpenOCD failures.
A nonzero status observed after helper-requested child termination is retained
with that provenance and does not by itself fail successful local client work.
Cancellation, missing one-shot result, infrastructure failure, and unconfirmed
required cleanup remain operation failures.

The following table defines the required outcomes:

| Active operation state | Later status check | Primary outcome |
| --- | --- | --- |
| Operation succeeds | No OpenOCD failure; cleanup succeeds | Success |
| Operation succeeds | Cleanup discovers OpenOCD `N != 0`; cleanup succeeds | OpenOCD failure `N` |
| Operation succeeds | Cleanup fails; no OpenOCD failure | Cleanup failure |
| Operation succeeds | Cleanup fails and discovers OpenOCD `N != 0` | Cleanup failure; `N` diagnostic |
| Operation already failed | Cleanup succeeds | Operation failure |
| Operation already failed | Cleanup discovers OpenOCD `N != 0` | Operation failure; `N` diagnostic |
| Operation already failed | Cleanup fails | Operation failure; cleanup diagnostic |
| OpenOCD `N != 0` already observed during operation | Cleanup subsequently fails | OpenOCD failure `N`; cleanup diagnostic |
| OpenOCD `N != 0` already observed during operation | Helper/forward failure follows | OpenOCD failure `N`; later failure diagnostic |

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

The Zephyr adapter passes probe selection and channel commands to the OpenOCD planners; probe
identity is not a session admission or reservation key. Neither the adapter nor
the generic session layer serializes session admission or lifetime because two
operations select the same physical probe. A session waiting for OpenOCD
readiness, running an operation, or cleaning up does not reserve that probe on
behalf of other runner sessions. Session observation locks and workspace leases
protect only their own session resources.

Helper deployment uses the separate synchronization boundary described in §35.
The deployment lock is released before the control helper starts and is not
held during OpenOCD acquisition or the session lifetime. The design does not
promise that every startup step is free of shared synchronization.

Whether channels on a physical probe are independently usable is determined by
the probe, its driver, and OpenOCD configuration. OpenOCD owns acquisition and
reports contention through its normal diagnostics and exit status. The runner
exposes that failure without adding a probe reservation service or a queue.
Hardware validation of simultaneous channel acquisition would test this
equipment/OpenOCD assumption; it is not needed to verify the runner policy.

### SSH loss

Fail the local operation after local SSH loss is detected. Clean the remote
session after the helper observes control-channel loss.

---

## 43. Code Ownership Boundaries

The physical Python tree in §5 is the current module layout. Logical ownership
is more durable than a duplicate path sketch:

- `config.py` owns configuration loading, schema validation, and selection
  resolution.
- `zephyr44/runner.py` adapts Zephyr runner state to board-independent plans.
- `remote/openocd_plan.py`, `debug.py`, and `flash.py` construct OpenOCD
  commands without owning their execution.
- `remote/paths.py` classifies required paths, and `remote/staging.py` builds
  staging manifests and local archives; the backend and remote helper carry
  out configured transfer and safe extraction.
- `remote/session.py`, `helper_client.py`, and `backend.py` coordinate local
  session lifecycle, helper protocol, and OpenOCD result propagation.
- `remote/model.py` defines transport and service data; `services.py`, `rtt.py`,
  `arguments.py`, and `tcl.py` own service, RTT, explicit session-value argument
  templates, and Tcl-word quoting.
- `forwarding.py` manages local SSH forwards to remote OpenOCD-owned listeners;
  `remote_helper.py` allocates remote addresses and checks selected ports for
  bind collisions.
- `remote/protocol.py` owns helper wire framing and validation. Diagnostics are
  produced and propagated at the subsystem boundary that observes each
  failure, rather than through a shared cross-cutting implementation.
- `remote_helper.py` owns remote supervision, output relay, protocol dispatch,
  and cleanup.
- `remote/ssh.py` is the only boundary for configured SSH command behavior.
- `remote/preferred_address_cache.py` owns the best-effort local preferred address
  cache; it does not own remote leases or external SSH sharing state.

Platform-specific SSH behavior, if any is eventually needed, shall remain inside the SSH transport layer rather than spread through runner logic.

---

## 44. Test Architecture

The maintained test suite separates self-contained unit tests, local process and
socket integration, Zephyr integration, SSH integration, and destructive
hardware validation. External layers consume explicitly configured environments
and local hardware inventory data. Recording mode remains free of SSH, helper,
OpenOCD, GDB, and hardware I/O. Hardware capabilities are selected
independently so an unsupported optional capability does not suppress other
operations. Verification traceability maps requirements directly to maintained
tests; this document describes only the architecture of that test boundary.
Generic `RemoteProcess`, helper/session boundaries, and socket-forwarding
interfaces provide deterministic seams for fake OpenOCD process and socket
endpoints, so remote-session and forwarding behavior can be exercised without
physical hardware.

---

## 45. Architecture Decisions

Selected for the target architecture:

- board-agnostic custom runner;
- no board/vendor-specific product behavior;
- Linux supported by one implementation;
- runner name `remote_openocd`;
- built-in `openocd` retained;
- per-user default runner selection;
- `default_runner` limited to `openocd` and `remote_openocd`;
- `EXTRA_ZEPHYR_MODULES`;
- self-contained Zephyr module;
- no pip/PyPI requirement;
- split Python implementation;
- Python setup script;
- commented configuration template;
- `openocd` default initially;
- normal build and reconfiguration detection for generated runner state;
- Zephyr-version-specific reuse of the non-private `OpenOcdBinaryRunner` interface only;
- configurable external SSH client command satisfying the SRS capabilities;
- default SSH command `ssh`;
- SSH command may contain fixed arguments;
- all SSH operations use the configured client abstraction;
- SSH sharing state remains externally managed, with a disposable local preferred
  address cache and authoritative remote lease/port validation;
- the SSH client continues to use its normal configuration;
- unprivileged remote helper;
- explicit path mappings with staging fallback;
- local GDB;
- remote OpenOCD;
- per-session remote loopback isolation;
- structured RTT handling;
- semihosting console via OpenOCD stdout/stderr;
- one concrete local `RemoteSession` owner;
- single-use session acquisition through `RemoteSession.open()`;
- no generic `SessionBackend`/`BackendSession` layer;
- cleanup-only `RemoteSession.close()`;
- genuine child result stored separately from shutdown trigger and infrastructure status;
- primary-failure selection by the active local operation;
- condition-driven bounded local shutdown after controller-input half-close;
- one remote phase-specific lifecycle authority with generation-tagged attempts;
- local launch gate, revalidated at actual execution entry;
- structured outcome and one immutable terminal snapshot;
- finite child-stream final observation at readiness determination;
- no controller-recognition fence for READY or retry;
- bounded local forwarding-process health polling;
- no persistent cross-session firmware or configuration cache;
- local and remote cleanup are bounded after their respective loss
  observations; detection timing and the interval between observations are
  delegated to the SSH and operating-system layers.

---

## 46. Redesign costs and retained complexity

| Category | Architectural effect |
| --- | --- |
| Eliminated complexity | Post-START STOP parsing, controller-recognition success fences, controller checkpoint/ACK at retry and readiness, and split ERROR/SESSION_CLOSED result reconstruction disappear at cutover. |
| Moved complexity | Cancellation safety belongs to the local launch gate; remote termination belongs to the remote authority. Exception-note result composition becomes structured outcomes and boundary rendering. |
| Inherent complexity retained | Native acquisition/handoff interruption, process-group identity and reaping, actual producer settlement, stream final observation, staging exclusion, independently owned forwards/RTT, and bounded failure reporting remain. |
| New costs | SSH directional-EOF qualification, phase-specific data and generation validation, revalidated queued launch, explicit residual-disposal evidence, structured terminal schema, and bounded final-scan policy need production implementation and regression coverage. |

The controller-lease runtime experiment is design evidence, not a production
implementation template. In particular, it does not authorize copying pidfd,
`/proc`, or subreaper machinery beyond existing qualified process ownership.
The [migration traceability](../traceability/verification.md#controller-lease-migration)
records intentional differences and unimplemented acceptance obligations.
The physical-boundary checkpoint follows the structured foundations. The current
Protocol v1 coordinator consumes the adapted physical owners; foundation and
physical-boundary tests do not establish Protocol v2 acceptance. Client/session
authority integration, the coordinated wire/controller-lease cutover, and removal
of Protocol v1-only machinery remain later work. No protocol cutover has occurred
at this checkpoint.
