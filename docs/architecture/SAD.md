# Zephyr Remote OpenOCD Software Architecture Document

## Navigation

- [Purpose, drivers, and overview](#1-purpose)
- [Module structure and setup](#5-self-contained-zephyr-module)
- [Zephyr runner integration](#9-module-discovery)
- [Remote execution planning](#18-generic-remote-session-model)
- [Services, SSH, staging, and lifecycle](#27-remote-openocd-service-isolation)
- [Errors, code boundaries, tests, and decisions](#42-error-handling)

## 1. Purpose

This document describes the deployed architecture for the Zephyr west runner
for remote OpenOCD. Separate remote and local lifecycle authorities implement
the controller lease and Protocol v1. Physical process, workspace, forwarding,
RTT, and transport owners retain cleanup responsibility throughout acquisition,
adoption, and disposal.
Maintained evidence, selected-profile qualification, and remaining acceptance
limits are recorded in [verification.md](../traceability/verification.md).

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
- preserve ordinary Zephyr west runner workflows;
- reuse of existing board OpenOCD configuration;
- concurrent remote sessions;
- user-selectable SSH client;
- minimal remote administration;
- maintainable Zephyr-version isolation.

The historical workflow behind several of these drivers is recorded in the
[project background](../project_background.md). That background is
non-normative and does not restrict supported deployment models.

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
to the helper and supervision layer. On the local side, the operation wait
([§39.1](#391-operation-cancellation-and-waits)) is the one POSIX/Linux-specific
mechanism: it composes descriptors with `select.poll`, observes local child exit
through pidfds (with a non-reaping `waitid` fallback), and owns the Python
signal wakeup descriptor. Setup and generic runner code avoid adding further
Linux-specific assumptions, so future native Windows work can remain localized;
native Windows execution is outside the current scope.

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

            controller/       local coordination, transport, and staging
            helper/           remote coordination and physical ownership
            protocol/         shared domain values and Protocol v1 codecs
            openocd/          pure command and operation planning

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

The setup script copies `resources/config.example.yaml` only when the target
configuration file (its path argument, otherwise the default path) is absent,
reports the created/reused status and both absolute paths, and prints guidance
for `EXTRA_ZEPHYR_MODULES`, editing the configuration, and running the
configuration validator. For the default path it creates the
`zephyr_remote_openocd` configuration directory with mode `0700`; it creates the
file with mode `0600`. Existing parents, directories, and files are never
chmodded. It does not edit shell startup files, repositories, or `.zephyrrc`.

Setup takes an optional configuration path and never consults
`ZEPHYR_REMOTE_OPENOCD_CONFIG`. Only the default path's product-owned directory
is created with mode `0700`; for any other path the parent directory must
already exist, and setup creates only the file with mode `0600`. For such a
path, setup prints the validator command with that path and the
`ZEPHYR_REMOTE_OPENOCD_CONFIG` export that makes runner commands and build
integration read it.
Keeping environment-variable selection out of setup and validation leaves one
rule: the scripts act on their arguments, while runner commands and build
integration honor the override (§15).

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

`scripts/user/validate_configuration.py` is an offline front end to this loader
and resolver. It validates its optional path argument, otherwise the product
default configuration path, and ignores `ZEPHYR_REMOTE_OPENOCD_CONFIG`; a
leading current-user `~` in the argument is expanded before the file is read.
With no selected remote it reports structural validity and available
definitions. It prints commands as argv and forwarded environment names without
values, and does not test local or remote resource existence.

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
runner commands; the setup script and the validator take a path argument
instead (§7, §8). During CMake configuration, that path is
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
commands. Compatibility is semantic rather than bug-for-bug: the adapter
preserves inherited option names, value forms, and their intended effect while
translating them into remote plans, except where a specific SRS requirement
defines different remote behavior. Built-in runner behavior that reports
success without performing a requested erase or verification, that ignores
`--erase` combined with `--verify-only`, or that ignores an explicit
`debugserver --serial`, is deliberately not reproduced;
REQ-FUNC-OPT-008 is the complete list of such semantic differences, and each
has focused tests. The differential comparison with the built-in runner classifies
differences: an unexpected difference fails and must be resolved as a
regression, a necessary remote difference, or a deliberate difference added to
REQ-FUNC-OPT-008. There is no option selecting literal built-in behavior. In
particular, the remote runner owns the remote
bind address and service and forwarding configuration because it must allocate
remote ports and construct SSH forwards before local clients can use them.

The Zephyr 4.4.x runner integration reuses `capabilities()` and the constructor.
It overrides
`name()`, `do_create()`, `do_add_parser()`, and `do_run()`. The parser override
delegates to `OpenOcdBinaryRunner.do_add_parser()` before adding `--remote`.
Constructor and version coupling is isolated in the `zephyr44` package: the
public `zephyr44/runner.py` facade exports the runner class implemented in
`zephyr44/_runner.py`.

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
the shared OpenOCD command planner in `openocd/command.py`, used by flash,
debug, attach, debugserver, and RTT. When supplied, the planner emits
`-c "set _ZEPHYR_BOARD_SERIAL <serial>"` before the board configuration `-f`
arguments, so those configurations can consume the variable while loading.
When `--serial` is omitted, the planner emits no serial-selection command and
introduces no serial constraint.

---

## 18. Generic Remote Session Model

The generic subsystem receives structured data, conceptually:

```python
SessionPlan(
    host=...,
    ssh_command=...,
    process=ProcessSpec(...),
    staged_files=...,
    forwards=...,
)
```

This subsystem has no dependency on a specific board or SoC.

### 18.1 Runtime Environment Forwarding

While constructing the immutable `ProcessSpec` for an operation, the
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
with erase requested require them; ELF operations omit explicit erase commands,
matching the inherited Zephyr behavior. A verification-only operation never
erases, so requesting erase with it is rejected rather than ignored. Likewise,
BIN verification or verification-only requires a verify command. The adapter
runs this pure validation before remote-home resolution, so each of these
failures occurs before any remote work (REQ-FUNC-OPT-008).

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

Structured runner state retains Zephyr's OpenOCD GDB-server base port and
client-selected target listener. OpenOCD receives `gdb_port <gdb-port>` in every
session command. For `debug`, `attach`, and `rtt`, GDB connects to
`127.0.0.1:<gdb-client-port>`, forwarded to
`<remote-session-IP>:<gdb-client-port>`. A distinct client port can select another
target listener, such as base + 1 in a multi-target configuration; it does not
remap the base listener. When distinct, the base listener also participates in
address-allocation preflight as a reserved service without a local forward.

`debugserver` launches no GDB client and ignores `gdb-client-port`. It forwards
`127.0.0.1:<gdb-port>` to `<remote-session-IP>:<gdb-port>` and reports that endpoint.
Forward acquisition and cleanup use the existing session resource owners for
both forms. The runner does not discover or forward every additional listener
that arbitrary OpenOCD configuration may create.

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
run local batch GDB for west rtt (cancellable setup work)
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

Connection setup registers each socket on its already-owned connection as it is
created and closes it on failure or interruption. It connects without blocking,
completes the connection through writability and `SO_ERROR`, retries refused
attempts after a short delay until the startup deadline, and allows a brief
grace period for the forward to report remote-channel refusal. Those are real
connection timers, not cancellation polling. A failed attempt is retryable only
once its socket is released. If releasing it fails, the attempt failure becomes
the operation's established failure, carrying the cleanup failure as diagnostic
detail as forwarding startup failures carry their rollback failures, and no
further attempt is made. The client's cleanup scope is
active before adopting the connected socket, accessing stdin, or delivering
initial output. A terminal-mode scope restores prior attributes before the
socket is closed. The relay owns the bounded pending-input queue and interprets
readiness, retaining unsent bytes across partial writes and transient
backpressure. It likewise owns pending channel output for a descriptor-backed
output stream: it reads the channel again only after earlier output is
delivered, and writes at most `PIPE_BUF` bytes to the descriptor once it reports
writable, which a writable pipe or socket accepts without blocking. Before
returning a session result, it gives output already accepted from the channel,
at most one chunk, one immediate delivery opportunity. Session completion does
not wait for a consumer that cannot take it then; that output is discarded, like
channel data not yet read when the session ends. The relay
does not use the stream's buffer or its lock, because helper output delivery
writes the same standard output from the reader thread and may itself be
blocked there. A stalled output consumer, such as a pager that stopped reading,
therefore pushes back on the channel inside the relay's wait, where a recorded
Ctrl-C takes effect, instead of blocking the relay in a write that the
non-raising capture would resume. Bytes that other code left unflushed in the
stream are not ordered before relayed output; helper output delivery flushes
each record. A terminal write may still wait briefly on the terminal; an
in-memory stream without a descriptor is written directly. It reads session status before
each wait and again at
receive-side channel EOF, so a recorded session exit takes precedence over that
channel closure. Each wait composes stdin, the channel, the output descriptor,
the session's state notifier, and owned SSH transport exits
([§39.1](#391-operation-cancellation-and-waits)).
Terminal and socket cleanup are attempted independently; an existing failure
remains primary, with secondary cleanup failures attached as notes.

The `rtt` command's batch GDB setup is not an interactive client interval. It
runs as a finite local process with inherited terminal I/O and `check_call()`
result semantics, so runner Ctrl-C cancels it like other setup work.

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
random candidates. A cached address conveys no ownership and cannot
override another session's lease.

The optional preferred candidate is additional to the 32-candidate random
allocation budget. Exhausting that budget fails startup without launching a
child. Candidate selection never restarts a spawned child (§38).

The helper reserves each candidate address by binding a Linux abstract Unix
socket whose name is keyed solely by that address. The kernel socket namespace
coordinates helpers across remote users, runtime directories, and helper
revisions within the same network namespace. Separate network namespaces have
independent loopback networks and independent leases. The helper temporarily
binds all initial and deferred service ports to check for collisions, then
releases the probe sockets before starting OpenOCD. The address lease remains
held until child cleanup completes, including when RTT forwarding is deferred;
it is released during session cleanup.
The kernel also releases it if the helper exits or is killed.

OpenOCD owns the actual enabled GDB, Tcl, telnet, and RTT listeners on the
allocated address; the helper neither creates nor probes those listeners.
The lease excludes other cooperating helpers, while unrelated processes remain
able to bind TCP ports. A post-spawn OpenOCD bind failure ends the operation;
the helper does not restart OpenOCD on a different address.

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

The controller uses explicit local forwarding specifications:

```python
ForwardSpec(
    name="gdb",
    local_port=...,
    remote_port=...,
)
```

These forwards connect local clients to remote listeners for:

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

`SessionPlan.forwards` holds initial `ForwardSpec` values, each describing a
local port and its remote listener. `reserved_listeners` holds `ListenerSpec`
values that participate in remote address allocation without requesting a local
forward. This includes a distinct GDB base listener and RTT deferred until batch
GDB setup succeeds. `SessionPlan.listeners` projects both sets into the helper's
wire-side listener contract.

The `auxiliary_forwards` subset identifies initial best-effort forwards; all
other initial forwards are required. This classification is internal to the
controller and is not part of [Protocol v1](protocol.md). Omitting it preserves
the generic all-required default. Debug plans classify enabled Tcl/telnet as
auxiliary and GDB plus explicitly requested RTT as required.

Immutable `ForwardSpec`, `ListenerSpec`, and `SessionPlan` construction validates
locally knowable names, ports, duplicates, listener conflicts, and auxiliary-set
relationships. Reserved listeners have no local port to validate. The helper
independently validates the serialized listener contract. Recording retains its
historical `reserved_services` JSON shape, with the remote port shown in its
legacy `local_port` field; no product configuration or wire key changes.

The session starts required forwards in one batch, then attempts each
best-effort forward in its own one-forward batch. Each manager call rolls back
that call's pending processes and preserves previously active forwards.
`ForwardStartError` exposes the failed forward, startup cause, and explicit
rollback cleanup errors. The session warns only when a best-effort attempt
rolled back successfully. Failed rollback remains fatal; pending processes
receive one bounded cleanup attempt and need not be adopted for a retry.
The manager owns the pending batch from transport registration through commit
or completion of each rollback attempt. Pending transports remain reachable by
`close()` if startup escapes before or during rollback. They are not published
as owned forwards, and another startup cannot begin until a stranded pending
batch has been cleaned. Close covers committed and pending ownership,
attempting a transport once if commit has temporarily recorded it in both sets.

Forward startup supplies its pending-set registration callback to the existing
`SshCommand.popen()` acquisition method. The transport invokes that callback
inside its rollback guard before returning, so a `BaseException` at the method's
return cannot strand a transport before caller assignment. Commitment records
committed ownership before releasing pending rollback ownership. Readiness
waits on the transport's output, its process completion, the readiness
deadline, and operation cancellation through the common operation wait.

Runner Ctrl-C never raises into startup rollback: a cancellation recorded while
rollback runs is retained and every pending transport receives its cleanup
attempt. A startup failure remains primary, with later cancellation retained as
diagnostic information.

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
Long-lived transports reach it through the non-shell exec launcher of
[§38.1](#381-local-ssh-subprocess-ownership), which replaces itself with that
unchanged argv in the same process.
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
this contract. The runner requires directional EOF without capability
negotiation or a configurable alternate shutdown mode. Under connection sharing,
EOF closes only the controller channel, leaving the external master alive.

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

`controller/preferred_address_cache.py` stores a disposable preferred address under
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
`protocol_v1/helper-<sha256>.py` beneath the per-user deployment directory.
It acquires an exclusive `fcntl` lock on
`protocol_v1/.deploy.lock` before checking, installing, refreshing, or
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

Deployment uses the `protocol_v1` namespace. The
content-addressed zip application includes the canonical outcome, lifecycle, and
wire modules, using empty package initializers to retain standard-library-only
remote execution. Its digest covers every bundled source, with no copied codec
or lifecycle implementation.
Client and helper are deployed as matching revisions, without negotiation or a
dual-protocol runtime. Configured argv, staging archives, and path templates
follow their respective contracts. A version change requires an incompatible
wire contract change.

No assumption is made that the local SSH executable comes from the local Linux distribution.

---

## 36. Remote Helper Protocol

The selected internal helper wire format and behavior are specified solely in
[protocol.md](protocol.md). The SAD records the architectural consequences of
that contract; it does not duplicate message fields, framing, state
transitions, ordering, validation, or frame-size limits. The deployed client
and helper implement one strict matching contract.
The shared wire validator checks both individual event structure and consistency
with previously observed acquisition evidence. Contradictory terminal disposal
claims are rejected before the helper client can publish a remote outcome;
they enter the existing reader/protocol-failure path instead.

## 37. Remote Session Storage

Preferred:

```text
$XDG_RUNTIME_DIR/zephyr_remote_openocd-sessions-v1/<session-id>/
```

Fallback:

```text
~/.cache/zephyr_remote_openocd/sessions-v1/<session-id>/
```

Workspace storage format 1 retains the residual-child marker described in
§38.7 and staging lease/closure metadata. The isolated roots scope workspace
ownership and conservative reclamation independently of content-addressed
helper deployment. Marker absence alone does not establish child disposal;
reclamation also requires positive inactivity evidence.

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
the local `SessionController` lifetime and closes it when the operation finishes.
The helper may also end the remote session after OpenOCD exit, protocol failure,
or control-channel loss. The ownership boundaries are:

| Owner | Resources and decisions |
| --- | --- |
| `SessionController` | Coordinates staging and subsystem cleanup and reports session/cleanup failures to the active operation. |
| `HelperConnection` | Owns the helper control channel, protocol reader, helper observations, output delivery, and helper shutdown. |
| `ForwardManager` | Owns local forwarding SSH processes, forward status checks, and forward cleanup. |
| `ManagedSshProcess` | Owns one local SSH subprocess and its stderr drain. |
| External SSH connection-sharing mechanism | Owns its master and any forwarding state retained independently of runner-launched subprocesses; outside runner cleanup. |
| `HelperSession` | Coordinates remote physical owners, observation dispatch, and cleanup through the remote authority. |
| `LifecycleAuthority` | Owns remote phase transitions, attempt admission, and outcome selection; acquires no resources. |
| Child physical owner | Owns the acquired OpenOCD process group, output relays, termination, reaping, and stream closure; reports facts to the remote authority. |

`SessionController.close()` coordinates the subsystem cleanup sequences and publishes
the failure selected by its shared `SessionAuthority` authority. Failure of one
subsystem cleanup does not prevent the other applicable cleanup sequence from
being attempted. Each owner cleans up the resources it acquired; no owner
transfers an active resource to another owner merely because cleanup encountered
an error.

Session closure does not depend on signal masking. Runner Ctrl-C is captured
without raising ([§39.1](#391-operation-cancellation-and-waits)), so it cannot
interrupt subsystem entry, the transitions between cleanup owners, or an
owner's cleanup sequence. Closure enters settlement first: a cancellation
recorded during cleanup is retained but never dispatched, and every applicable
bounded cleanup attempt runs. Arbitrary `BaseException` from a cleanup step is
recorded and does not prevent the next independent owner's attempt.

Interactive GDB retains the separate signal semantics described by
REQ-FUNC-HELP-013.

### 38.1 Local SSH subprocess ownership

`SshCommand.popen()` starts each long-lived control or forwarding transport with
SIGINT blocked and ignored, while preserving the caller's controlling terminal and
foreground process group for interactive SSH authentication. The launch thread
temporarily blocks SIGINT so the child inherits that mask across exec, then
establishes managed ownership and starts the stderr drain before restoring its
exact previous mask. The drain inherits blocked SIGINT and stops through pipe EOF
and explicit cleanup. Existing threads' masks remain unchanged. Keeping the
transports in the foreground group preserves terminal authentication without
introducing background-group SIGTTIN reads. This mask is child signal topology:
it is the one retained SIGINT mask, and it does not protect the parent.

The ignored disposition comes from a small exec launcher: the runner's own
interpreter, started isolated (`-I -S`), sets SIGINT to `SIG_IGN` and then
`execvp()`s the unchanged SSH argv, so the launched PID becomes the SSH client
and cleanup signals it directly. A Python handler would reset to the default
disposition at exec, and switching the runner's process-wide disposition around
launch could discard a runner Ctrl-C, so the disposition is set only in the
child. OpenSSH installs its terminating SIGINT handler only when SIGINT is not
already ignored, but 9.6p1 and 9.7p1 unblock an inherited SIGINT mask; the
ignored disposition covers those releases, and the mask still covers clients
that replace the handler unconditionally. The managed process reports the
configured SSH argv, not the launcher. An SSH executable that cannot be
executed is reported asynchronously: the launcher writes a diagnostic naming
the configured command to the transport's stderr and exits with status 127,
which follows the existing transport-exit rules. Failure to start the launcher
itself remains a synchronous launch error. Runner
Ctrl-C delivered during launch only latches the operation's cancellation fact
([§39.1](#391-operation-cancellation-and-waits)); the launch first passes the
operation's effect-admission check and then completes into its owner.

The transport wrapper is resource-free and owned before `Popen` begins.
Its private `Popen` subtype registers the resource-free raw instance with that
wrapper before invoking the standard-library initializer. This reference alone
is not a usable process cleanup handle until CPython publishes the PID; because
runner Ctrl-C is captured without raising, no runner-owned asynchronous
interruption occurs in that internal interval. Once the initializer returns, the wrapper reaches the subprocess
and pipes even if any `BaseException` interrupts the return, normal managed-owner
assignment, or subsequent setup. The contract does not claim recovery from an
arbitrary exception synthetically injected inside CPython before PID publication.
Rollback checks whether the raw instance has a child PID before attempting
process disposal; normal pre-publication constructor failures retain
standard-library cleanup. If failed fork/exec initialization has already
reaped the child and closed every pipe, with no stderr drain acquired, rollback
preserves the original launch exception. This subtype is part of the existing transport
acquisition path, not a separate acquisition API or lifecycle authority.

Acquisition then records the stderr drain before starting its thread. Before a
drain exists, rollback closes the raw stderr pipe directly; after drain adoption,
only the drain closes that pipe. The drain decides whether its reader may hold
the stream from the thread's own started state, not from a flag set after
`Thread.start()` returns, so a failure at that return still gets bounded reader
settlement instead of a buffered close that the reader's read could block. Constructor and thread-start failures use the
same bounded process termination and independent stream cleanup as other
acquisition failures. They require no replacement wrapper or drain. The
resource-free wrapper has one acquisition path, populated by transport launch.
A failure while restoring the launch thread's mask cannot replace an already
established acquisition failure; it is retained as secondary detail before
rollback.

The existing `popen()` method accepts a caller ownership-registration callback.
The helper registers the transport on its already-owned client; forwarding
registers it in the manager's existing pending rollback set. Registration runs
inside transport rollback before returning, and failures during registration
still dispose the same managed transport. The return value observes an already
registered resource rather than transferring its sole cleanup reference. This
closes project-controlled caller handoffs for any `BaseException` without
another acquisition API or lifecycle authority. Direct low-level callers that
use the return value must provide registration when that handoff requires
continuous cleanup ownership.

The inherited mask and ignored disposition are launch-time mitigations, not
unconditional SIGINT isolation. Terminal SIGINT intended for GDB leaves a
transport unaffected while its client either retains SIGINT blocking or leaves
the ignored disposition in place. The executed client or wrapper can install
its own handler and unblock SIGINT, and may then terminate. SRS §2.8 does not
require clients to preserve either, and this behavior is not an additional SSH
compatibility requirement. The terminal SIGINT integration test exercises
synthetic transports that reset the disposition while retaining the mask, and
that unblock the mask unless SIGINT is ignored, as OpenSSH 9.6p1 and 9.7p1 do;
it does not establish survival for all compatible SSH clients.

If a client terminates on SIGINT, resulting control-transport or required-forward
loss follows the existing session-failure and bounded-cleanup rules. Loss of a
best-effort forward follows the existing warning policy. The runner does not
transparently reconstruct an interrupted debugging session.
Explicit lifecycle cleanup still terminates and reaps each owned SSH process
directly. Any `BaseException` after mask restoration, including from caller
registration, rolls back the managed process before propagating; the ownership
transition and return remain inside the rollback guard.

`ManagedSshProcess` remains a narrow ownership wrapper rather than a session
abstraction. It delegates process status and termination to the underlying
SSH subprocess and owns exactly one `StderrDrain`. Standard input and output
remain available to the session protocol or forwarding-startup owner, while
the stderr pipe is detached from the subprocess object and transferred to the
drain so that it has only one local owner.

The current design uses a per-process drain thread because a long-lived SSH
client may emit more diagnostic data than an operating-system pipe can hold
while its stdout still carries protocol or readiness data. The drain retains
only a bounded byte tail. A lock protects that tail because failure observation
may read it while the drain thread is still appending data.

Shutdown first gives the drain a bounded opportunity to reach EOF, so final
diagnostics are retained, and then closes the stderr stream. The reader waits
for readiness on the raw descriptor together with a drain-owned wakeup and
never holds the buffered stream's lock while blocked. EOF can be withheld by a
writer the runner does not own: a connection-sharing master keeps the stdio
passed by its client until the remote channel closes, and a descendant can
inherit the pipe. When EOF does not arrive within the opportunity, cleanup
stops the reader through its wakeup and closes the stream and wakeup without
reporting failure; diagnostics are best effort. Only a reader that does not
stop within a second bounded budget is reported as a cleanup failure, with its
stream retained, because closing a buffered pipe while another thread uses it
can block on the stream's internal lock. Close serialization and an explicit
closed flag keep repeated cleanup attempts harmless. The drain thread is a
daemon so an unresponsive reader cannot hold local process shutdown open
indefinitely.

This separation is intentional: `HelperConnection` and `ForwardManager`
perform process cleanup within their own subsystem sequences,
`SessionController.close()` coordinates those cleanup sequences, `ManagedSshProcess`
exposes per-process control and diagnostic access, and `StderrDrain` alone
owns stderr consumption and stream closing. In this design, removing the
wrapper, drain thread, bounded tail, or bounded reader shutdown would require
another mechanism that preserves those ownership, backpressure, diagnostic,
and bounded-shutdown properties; simply removing them would introduce dual
ownership, permit pipe backpressure to stall the session, lose actionable SSH
diagnostics, or make cleanup potentially unbounded. The per-process wrapper
does not replace the helper and forwarding resource owners.

`SessionController` is the sole local remote-session coordinator and owns all
runner-acquired local session resources, excluding externally retained sharing
state as described in §32. `SessionController.prepare()` deploys the helper revision
and returns a coordinator with no live session resources. The adapter adopts
that coordinator before calling its instance `acquire()` operation, and
releases it through cleanup-only `SessionController.close()`. There is no factory
returning an acquired session: the caller must own the resource-free coordinator
and enter its cleanup scope before calling `acquire()`. A callee's rollback
guard cannot cover interruption after returning a live object but before the
caller adopts it. The helper client and
forward manager own their respective resources and cleanup sequences beneath
this boundary. The session stores a resource-free `HelperConnection` before calling
its instance `acquire()`. The client's registration callback stores the control
transport on the client before `SshCommand.popen()` returns. The client then
registers and starts one protocol reader thread that owns the helper's event
stream until shutdown; registration precedes start, so a `BaseException` after
start leaves that reader owned for shutdown and no second reader can begin. The
reader reports `SESSION_CREATED`, `READY`, and every later event to the shared
session authority. The coordinator never reads the stream: it waits for the
allocation (with the startup deadline) and later for READY on authority state
notifications through the operation wait.

The local reader exclusively uses nonblocking `os.read()` from the beginning
of ownership; it never reads through `Popen.stdout`'s Python buffer. It feeds
`ControlFrameBuffer`, then applies the canonical decoder and `EventOrder` before
dispatching each complete frame. Each ordinary wait observes both stdout
readability and the owned control process through `wait_for()`'s non-reaping
completion source. One raw read of at most 64 KiB returns to that common wait;
continuous stdout cannot prevent observation of process completion. EOF and
process completion are independent facts. EOF without terminal information
remains reader failure. Because EOF also ends the sole protocol stream, the
reader enters the same two-phase observation described below as soon as a read
returns EOF: admission is revoked before the framer is finished or stderr
diagnostics, which may wait for the drain, are collected. A terminal already
recorded keeps its meaning, and otherwise the reader failure is selected when
observation finishes.

When the wait reports control completion (including coincident readability),
the reader calls `SessionAuthority.begin_control_exit()`. Under its existing
lock the authority records transport end and a pending final observation, then
notifies and releases the lock. START and dependent admission are immediately
revoked, but a refusal issued while the observation is pending does not select
the operation failure. The authority retains that refusal and records it after
final accounting, behind any terminal or control-end failure, so it becomes
primary only when accounting established no failure. Callers record it through
the ordinary failure path; they need no pending-specific handling.
Acquisition/readiness waits cannot consume SESSION_CREATED or READY
while the observation is pending; absent an established failure, they continue
through the ordinary notifier wait. Foreground/session waits observe the helper
only through reader/authority notifications; control-process completion belongs
to the reader's wait sources, while forwarding processes retain coordinator exit
wakeups. This also applies before the reader recognizes control exit: if ordinary
output backpressure stalls that reader, the coordinator waits for notification
rather than repeatedly waking on the same unreaped control process. Callback
release resumes observation, and native cancellation still wakes the coordinator.
Existing cancellation dispatch and semantic startup deadlines still apply.
Successful terminal-result projection also waits for final accounting, while
established failures remain publishable immediately.

The same sole reader performs the finite nonblocking final scan outside the
authority lock. Each valid frame uses the ordinary ordered recording/dispatch
paths. Output and ATTEMPT callbacks may perform external I/O, but never hold the
lifecycle authority lock: their backpressure can stall the reader without
preventing cancellation/failure recording or settlement entry. No callback has
an end-to-end wall-clock guarantee. A scan failure is recorded through the normal
reader-failure path; a `finally` calls `finish_control_exit()` to publish scan
completion. Under the authority lock, finish records a generic control-end
failure only if no valid terminal was obtained, clears the pending observation,
and notifies. Earlier failures, including cancellation committed during a
blocked callback, retain precedence over later terminal or transport detail.
This two-phase physical observation uses the existing lifecycle authority and
does not wait for another observer's acknowledgement.

The final scan has a non-replenishing allowance of `MAX_FRAME_SIZE` (one MiB)
of new raw bytes, in reads of at most 64 KiB. This reuses the remote readiness
checkpoint's semantic observation bound (§38.4): it can complete any one legal
maximum-sized frame, including a prefix retained from ordinary reading, without
promising to drain an arbitrary backlog. Previously retained bytes precede new
bytes and are not charged again. Every complete valid frame within the cut is
accounted. Would-block and actual EOF both finish the scan immediately: an empty
framer is complete, and a remaining prefix is a framing failure. Budget
exhaustion ends the scan with infrastructure uncertainty, even at a frame
boundary; it does not assert EOF or falsely label a cut prefix as malformed.
Oversize, malformed, and illegal-order frames use the ordinary reader-failure
path. Frames after the first invalid frame are not accepted.

After final accounting, the reader publishes physical reader completion and
notifies the coordinator. A registered reader whose `start()` failed before it
ran likewise publishes that it has no remaining opportunity to report protocol
facts; a start failure after actual launch leaves publication to the reader's
normal `finally`. Runtime `observe_status()` defers `Popen.poll()` until no reader
opportunity remains, so final terminal detail is accounted before a control-status
failure can be selected. Nonzero status remains independent secondary evidence
after a reader-start failure. Only the existing coordinating process owner calls
`poll()`/`wait()`; the reader never reaps. During existing bounded shutdown, the
owner may settle the process before the reader finishes, but defers recording
its status until reader settlement. Reader registration, bounded joining, stream
closure, and the stable completion descriptor retain the existing client cleanup
owner. The client acquires that descriptor during transport registration before
launching the reader. Reader waits borrow it; cleanup releases it only after
reader settlement. Other common waits own and close their transient pidfds.

SESSION_CREATED and READY remain valid protocol evidence at the final boundary,
but the durable transport-end fact prevents their use for acquisition, START,
or dependent admission. Even zero control status without a valid terminal is
infrastructure failure. A valid terminal keeps its remote outcome and genuine
child result; a nonzero control status remains independent failure evidence.
An already-established operation failure remains primary. Otherwise a detailed
terminal failure encountered in the finite scan precedes generic transport-end
or control-status evidence; malformed trailing data and later transport failures
remain secondary. A terminal beyond the finite cut is not promised recovery.
These rules require no grace timer, future bytes, or pipe EOF. The cut may
include arrivals during the scan and exclude bytes beyond the allowance; it
makes no claim about physical ordering of remote exit, local process exit,
pipe writes, pidfd readiness, or scheduling.

START is accounted against facts the reader already recorded, admitted as a new
effect, and written to the control pipe without blocking on its backpressure.
When the pipe would block, the common operation wait observes both writability
and authority notifications, with its existing cancellation dispatch. Before
each partial-write attempt, the coordinator observes settled control status and
asks the session authority to admit further START bytes. An established failure
is published immediately. Otherwise pending control final observation pauses
delivery on authority notification and cancellation alone, without watching
stdin writability. After final accounting, a terminal or control-end fact stops
START with a session-ended error even when the remote outcome was successful.
A write into helper input that has already closed is likewise not itself the
START failure: delivery stops and waits on authority notification until final
accounting selects the failure. This preserves detailed final-failure precedence and prevents session end from
leaving START blocked on unread stdin. Failure composition and cleanup retain
their existing authority and owners.
If acquisition
ends before the allocation is accepted, client cleanup terminates and reaps the
owned control process while closing its input lease and reading any available
final outcome. An owned client with no acquired transport closes harmlessly. Helper acquisition and
startup rollback belong to the session owner; `HelperConnection` has no
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

The lifecycle model in
[`helper/lifecycle.py`](../../python/zephyr_remote_openocd/helper/lifecycle.py)
implements a pure transition authority with immutable phase and attempt records.
Its read-only request role keeps the validated concrete request available to the
coordinator without coupling the model to Zephyr or physical owners. Attempt
entry and READY consume admission facts; child observation records the first
genuine status and the preceding cleanup-signal context. Settlement consumes
separate producer/resource disposal facts. Termination
prevents further entry or activation, including after a late acquisition result.
Closure freezes one snapshot; later failures remain local diagnostics.

The deployed helper's coordinator uses this authority for START, attempt entry,
adoption, marker evidence, readiness, observed exit, termination, and freeze.
Its observers report immutable facts; physical cleanup facts stay with their
owners. Physical adapters must validate eligibility before wire admission,
retain immediate acquisition ownership, and establish the facts passed into the
model. Boolean admission/liveness/settlement facts are not physical effects or
proof of adapter correctness. Observers report facts directly to the authority.

[`protocol/outcome.py`](../../python/zephyr_remote_openocd/protocol/outcome.py)
provides immutable diagnostics, child results, outcomes, disposal reports, and
terminal snapshots. Existing exception notes can be captured at a boundary as
nested diagnostic values without mutating the source exception. The first
failure stays primary and subsequent failures retain order and nested detail.
Helper/SSH status is validated independently, and wire encoding is bounded.
Flash and debug plans serialize explicit completion policy in Protocol v1 START.
Flash starts its event reader immediately without waiting for READY; its genuine
child exit is the operation result. Live operations wait for current READY.

One remote coordinator decides lifecycle transitions. Control-input, stream,
child-exit, deadline, writer, and signal adapters report facts; none independently
chooses shutdown or success. A standard-library asyncio structured task
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
Starting(immutable request, current attempt, readiness evidence)
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
settlement. One START admits at most one child attempt; the session supplies
correlation. Queued child facts carry the owned child identity, and phase
checks prevent late observations from reactivating a terminating session.
Rejecting an observation does not establish physical disposal.

At actual attempt entry, the authority validates the Starting phase, admits
an immutable exact-argv ATTEMPT diagnostic, establishes acquisition ownership,
and invokes spawn in the same effect boundary. There is no asynchronous effect
command queue between that validation and spawn. Failed diagnostic admission
prevents spawn. Admission means bounded local writer acceptance, not peer receipt.
Native signal-safe acquisition remains necessary even in a section with no
`await`.

### 38.3 Startup failure and readiness decisions

The deployed coordinator attempts child creation at most once per START. A
`live_server` child exiting before READY is a startup failure. A `process_exit`
child's exit supplies the one-shot result. Pre-spawn address-candidate selection
remains available for both policies.

Output text has no trusted bind-failure provenance, and even genuine EADDRINUSE
may follow opaque/user Tcl and adapter/target initialization. Repeating OpenOCD
would repeat those effects. Cleanup settlement proves resource disposal, not
side-effect repeatability, so the helper neither classifies output as permission
to retry nor restarts a spawned child. Startup exit is committed as a failure
when observed; later cleanup failures remain diagnostics.

The authority revalidates Starting state and absence of its own terminal
decision at entry. It accounts already-recorded fatal facts before READY.
EOF occurring, the control reader recognizing it, publishing it, the authority
dispatching it, and terminal freeze are distinct stages. The reader parses
`START [RELEASE] EOF` with the bounded frame buffer across arbitrary read
boundaries. It records whether RELEASE preceded EOF only as parser state; at
recognition of EOF after START without RELEASE it retains one monotonic
controller-lease-loss fact before the queue publication that can block, as
observer failures are retained before theirs. The coordinator accounts that
fact once, as a `controller_eof` termination with `CONTROLLER_LEASE_LOST`, at
the same points as other recorded fatal facts: its loop, attempt entry, both
READY checks, attempt settlement, and immediately before freeze. A trigger
already committed is kept and the loss joins the outcome under the ordinary
first-failure rule, behind an earlier natural failing child result. READY
can precede recognition of unread EOF and authority handling of released EOF;
it is never admitted after unreleased EOF was recognized, and after termination
commits READY cannot be newly authorized. The reader is cancelled before freeze,
so recognition cannot follow it; bytes it never read are not classified.

Local cancellation revokes dependent launch, and remote handling of controller
loss cleans all owned work. These separate authorities do not require a global
recognition order. Fatal physical observer/writer errors become failures,
and established primary failure is preserved; their reports must not be lost
merely because a task is cancelled before queue publication. Already recorded
fatal physical failures are accounted before READY or terminal
publication, even if ordinary dispatch is pending. This local failure check
requires neither future controller input nor recognized-EOF precedence.
Physical ownership, producer settlement, and phase validation remain mandatory.

READY admission requires `live_server` policy, Starting state, an owned child,
required evidence, and a final live-child check.
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
| Local SSH Popen | Prepared managed transport inside the configured launch guard | Raw acquisition populates that existing owner before drain construction; launch or mask-restoration failure terminates/reaps and independently closes owned streams, using raw stderr until a drain owns it. |
| Workspace, lease metadata, session lock | Allocation rollback scope, including partially created artifacts | Remote coordinator adoption transfers all artifacts; setup failure attempts workspace and lock cleanup independently. |
| Remote address lease | Current attempt acquisition scope | Transfer to child attempt only with continuous reachability; failed materialization/spawn releases it, normal cleanup releases it after child settlement. |
| Child Popen and its pipes | Acquisition ticket preregisters the resource-free raw Popen instance before initialization | Once the PID is published, rollback can reach the child even if constructor return fails. Child physical owner adopts inside that same guard; interruption on either side disposes the group, reaps, and settles pipes. |
| Observer tasks and protocol writer | Task-creation/setup scope | Register with session/attempt owner before publication; setup failure joins/cancels under bounded policy and retains unresolved obligations. |
| Staging shared lease | Standalone staging scope | Held through validation, extraction, and success reporting; always released on failure/process exit, with no transfer to the controller. |
| Forwarding transport | Forward manager's pending-resource owner | Keep rollback ownership through validation/adoption; independent failed forwards cannot orphan an acquired process or roll back another active forward. |
| RTT socket and terminal mode | Caller-owned resource-free connection scope and terminal scope | Each socket is registered on the already-owned connection as it is created; connect and initial observation populate that scope without returning a live socket. Terminal restoration and socket closure are independently attempted on failure/interruption. |
| Local finite command (`SshCommand.run`/`run_stream`, batch GDB) | Resource-free finite-process driver, created before launch | Admitted before launch; a private `Popen` subtype registers the raw child on the driver before the standard-library initializer runs, so any `BaseException` after the child is published, including at the constructor's return, stops and reaps it and closes its pipes. Failed exec that already reaped the child keeps its original error. An interrupted command is never replayed. |
| Helper protocol reader thread | `HelperConnection`, registered before start | Shutdown joins it within the bounded budget before closing the stream it reads; a reader that never started holds nothing. |
| Control completion pidfd | Resource-free `ProcessCompletion` already owned by `HelperConnection` | Acquire during transport registration before reader launch or coordinator reaping. Waits borrow the stable descriptor; cleanup closes it independently after reader settlement, retaining it if the reader remains live. |

A `SupervisedProcess` owns the configured OpenOCD process-group resources and
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

The owned raw reader's checkpoint performs a finite available-byte scan. Each
stream's reader consumes until EOF, a would-block result, or a raw read budget
of one MiB initialized when the checkpoint is requested; continuous arrivals cannot
replenish that budget. This budget matches the maximum control frame and is an
internal observation bound, independent of the startup timeout. Bytes read before
the checkpoint request are not charged against its raw read budget.
They retain their order ahead of later bytes in the same stream. Decoding and
ordered publication remain on the normal observation path. The coordinator
continues dispatching while checkpoint facts are published, then decides readiness from
child status and retained evidence. Reader cancellation or close still
settles the checkpoint. Readiness scans only child streams; controller input is
observed independently.

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

Reader and writer descriptor mode acquisition rolls back an effect that succeeds
before constructor adoption fails, preserving the initiating exception and
rollback detail. Writer close releases retained frames independently of
mode-restoration failure. The
signal scope owns both installed handlers and a plain first-signal latch;
handlers report facts without deciding transitions or mutating asyncio queues.
READY admission/activation and terminal freeze block native SIGINT/SIGTERM
delivery, account already-latched failures while blocked, and revalidate before
committing. Restoring the previous mask delivers signals arriving during the
blocked interval after that commit. Terminal cleanup inspection precedes this
boundary, so signals latched during inspection belong to the frozen outcome.
Signal-handler, descriptor, workspace, and allocation rollback cleanup continues
across `BaseException` failures while preserving the primary outcome and nested
secondary diagnostics on the structured Protocol v1 boundary. Failed retirement
of an address lease fails cleanup and leaves its cleanup owner reachable.
Address allocation likewise retains its reservation until all temporary TCP
probes have closed and the return value can adopt it. A probe finalizer or return
construction failure attempts every independent socket release and prevents
candidate retry when cleanup remains unsuccessful. Ordinary pre-spawn candidate
selection never repeats spawned child work.

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
shutdown; controller EOF does not imply a fabricated code.
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

The standalone helper connects its physical owners to the canonical remote
lifecycle authority. A session-owned acquisition ticket is reachable before
`Popen`, retains the raw process through supervisor construction, and retains
the supervisor through coordinator
adoption. Both construction rollback and an interrupted return use this same
cleanup reachability; partially adopted streams still receive owned observers.
Native handlers latch facts throughout those synchronous effects.

Process-group cleanup progress is represented by its owned task or the
coordinator-dispatched completion fact, rather than separate started/completed
flags. The TaskGroup owns successful task creation immediately, and the
coordinator adopts the task before auxiliary observer setup. If task creation
fails, the coordinator performs the same bounded disposal attempt directly and
accounts its result without publishing into the queue it normally consumes.
Output observer setup attempts both streams independently; an unavailable
observer cannot prevent process termination or leave shutdown waiting for an
EOF it cannot report. Its pipe remains child-owned until settlement. Failure
to establish the output-drain deadline records cleanup failure and ends that
drain opportunity, allowing owned observers and pipes to be settled.

Physical settlement records producer return, verified group disappearance,
joined observers, and closed pipes separately. Finishing a cleanup coroutine
does not confirm disposal. The canonical lifecycle settlement transition requires
these completed physical facts. Before cleanup signalling,
the process owner observes genuine leader status without reaping its identity;
delivered termination is recorded independently from that result. Group signals,
finite escalation, reaping, and disappearance checks use native OS
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
Reclamation uses explicit non-following filesystem inspection. Only
`FileNotFoundError` establishes marker, lock, or corresponding workspace
absence; other inspection errors retain the entry. A non-regular session lock
likewise does not establish inactivity. When a session lock exists, reclamation
inspects the residual marker after acquiring that lock, so an owner publishing
the marker before releasing ownership cannot be mistaken for a settled child.
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
The physical cleanup owner supplies residual-disposal evidence; the lifecycle
authority consumes it when deciding settlement and workspace eligibility.

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

Intentional local shutdown releases the controller lease: RELEASE, then
controller-input EOF. The helper's
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

The launch model in
[`controller/admission.py`](../../python/zephyr_remote_openocd/controller/admission.py) expresses
local launch eligibility within the synchronized `SessionAuthority`. It
requires observed readiness and phase-specific forwarding, preserves
cancellation and fatal failure across boundaries, and rejects stale launch
tickets when GDB setup is followed by RTT client execution. Its pure tests
establish these transition semantics.

The shared session authority lock serializes recorded terminal and
reader failure facts with cancellation and actual dependent-work entry. The helper
reader can record a fatal fact after a session check returns but before gate
entry. Checking the helper and then entering an independent gate does not
account for that already-recorded fact at actual execution entry. The
coordinator therefore records both through the same session lock before
committing execution entry. Regression coverage must synchronize the concurrent
reader observation with launch entry to qualify this boundary; synchronous
session closure during observation exercises only direct gate cancellation.

The same authority records every fatal local observation: terminal outcome
(including genuine nonzero child status), reader/protocol failure, helper/SSH
status, required forwarding failure, failed acquisition or dependent work,
local cancellation exceptions, and shutdown or cleanup failures. Each source
records at recognition rather than independently projecting precedence through
a return code or a cleanup result. The first recorded fatal observation remains
primary. Later failures persist as ordered diagnostics, including their existing
nested diagnostics; status checks and finalization cannot change that selection.
`SessionController` creates this authority and supplies it to `HelperConnection`;
the connection cannot create a separate arbiter. Helper shutdown records failures
directly into that authority and returns no result for its caller to arbitrate.
Public operation status is projected by the session after collecting physical
observations, rather than by the helper resource owner.
Best-effort forwarding remains advisory unless its owned rollback fails or an
already-established session failure requires retaining it as secondary detail.

The immutable remote terminal snapshot remains factual provenance. The child
return-code property exposes that fact without claiming operation success.
Operation status instead raises the authority's selected failure, including a
child-only failure; it never suppresses that failure to return a nonzero numeric
status. Health collection records helper/SSH and all forwarding observations
before a single synchronized status projection. Forwarding reports each exit
before polling another process, so an exception in a later poll cannot discard
or acknowledge an unpublished earlier exit. A terminal fact recorded during
collection is accounted by that projection. Every recorded fatal observation
also closes dependent-work admission.

Delivery acknowledgement is separate from failure retention. Repeated cleanup
does not replay an already-delivered failure unless newly recorded secondary
information reopens delivery. `snapshot().failure` inspects the retained failure
without acknowledging it; `publish_failure()` explicitly acknowledges delivery,
and `take_unreported_failure()` delivers only when acknowledgement is pending.
Reader failures use the same keyed failure record as every other source, retaining
their original exception as the cause rather than in separate reader state.
Helper cleanup records facts without acknowledging delivery on behalf of the
session. The session's finalization publishes
the complete authority result after independently settling its owned resources,
regardless of earlier delivery. The Zephyr adapter records local operation
exceptions and asks the session to finalize; it does not reconstruct precedence
from copied child status or choose between operation and cleanup exceptions.

GDB/RTT entry checks helper and forwarding health, then admits the effect: the
operation's cancellation read is the final condition evaluated inside that
shared authority's entry, immediately before the synchronous launch callback. Interactive GDB runs
through Zephyr's `run_client` inside the interactive ownership scope of
[§39.1](#391-operation-cancellation-and-waits); RTT batch GDB setup and the RTT
client are ordinary cancellable work. Cleanup ownership protects interruption
rollback. RELEASE followed by controller-input EOF ends the remote lease;
EOF without RELEASE is remote lease loss.

New forwarding work uses the same synchronized authority entry as GDB/RTT.
Immediately before starting a required forwarding batch or each best-effort
forward, the session accounts recorded terminal/reader failures, cancellation,
current READY, and the already-required forwarding for the current phase. The
new forwards become required only after successful acquisition; entry does not
require them to exist yet. Authority rejection propagates as the session outcome,
including for best-effort forwards, rather than being reduced to a forward advisory.
A terminal fact racing after committed entry may let that transaction finish
acquisition or rollback; its resources remain with the forward manager and
session cleanup owners. A later transaction must enter again. In particular,
RTT forwarding after batch GDB cannot start when the helper terminal was already
recorded during GDB, and a hypothetical later forward failure cannot displace it.
If a committed forwarding transaction fails, its failure publication consults
the same authority after rollback. An established terminal or reader failure
remains primary, with the forward failure and its rollback diagnostics retained
as secondary details. This applies to best-effort forwards too: an established
session failure cannot become an advisory. The authority lock does not span
blocking SSH acquisition or rollback.

The local operation alone owns dependent launch eligibility. Its conceptual
phases are Opening, Active, Cancelling, and Ended. These roles apply to each
dependent-work boundary, including forwarding, GDB setup, and an RTT client; an
RTT phase transition does not reopen a cancelled operation.

```text
prepare and adopt resource-free coordinator
    -> acquire helper transport and session workspace
    -> stage inputs; send immutable START
    -> keep helper stdin open as controller lease
    -> observe ATTEMPT diagnostics and remote READY
    -> validate forwarding entry; establish required forwarding
    -> validate local launch eligibility at actual entry
    -> run GDB / RTT / foreground server / one-shot result wait
    -> commit local closure; write RELEASE if still needed; close helper stdin
    -> read final output/outcome and settle owned local transports
```

The adapter owns the prepared coordinator before acquiring live resources and
finalizes it from the same exception scope on success, failure, or interruption.
The coordinator in turn owns helper and forwarding owners before acquisition.
Pending-resource owners provide rollback for forwarding and RTT throughout
adoption. No live resource is handed through an unguarded return interval.

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
launch. If START was completely written and neither a terminal nor control end
has been recorded, it makes one nonblocking write of the RELEASE frame, no
larger than `PIPE_BUF`, so the local SSH input pipe receives all of it or none. That is
a local pipe property only; remote framing tolerates any fragmentation. The
write is not retried or acknowledged, and its failure records no operation
failure: an unreleased EOF reaching a live helper is reported by the helper as
lease loss, and an ended or unreachable helper is accounted through terminal,
missing-terminal, and helper/SSH status. Closure then closes helper stdin once,
retaining stdout/stderr readers for final output and SESSION_ENDED. A shared
15-second coordination budget covers waiting
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
an infrastructure failure without changing its child result. The remote
`controller_eof` trigger reports that the helper ended on input EOF; the helper
classified it when its reader recognized it, and lease loss arrives as the
snapshot's `CONTROLLER_LEASE_LOST` failure. The local authority interprets the
snapshot like any other: it never reclassifies controller EOF from whether the
snapshot arrived before or after local closure began, because receipt time is
not remote observation time. The local error follows the snapshot's
precedence: a primary failure leads with a genuine nonzero child result noted
behind it, and a natural failing child result without one leads with its
secondary diagnostics noted behind it. Local timeout may
leave remote cleanup unconfirmed even after local SSH is reaped. External
sharing masters are never terminated by session closure.

Repeated closure is harmless; cleanup errors do not require automatic retry.
The established active-operation failure remains primary, with later shutdown
and child-result diagnostics retained. During an interactive local client,
fatal session facts are recorded and acted upon at the next defined status check
after that client returns; asynchronous interruption of GDB is not required.

### 39.1 Operation cancellation and waits

[`controller/cancellation.py`](../../python/zephyr_remote_openocd/controller/cancellation.py)
and [`controller/waiting.py`](../../python/zephyr_remote_openocd/controller/waiting.py)
make runner Ctrl-C an event the operation dispatches at explicit boundaries,
rather than an asynchronous `KeyboardInterrupt` that may arrive at any
instruction of resource-management code.

**Operation cancellation owner.** `operation_scope()` creates one
`OperationCancellation` for a live runner operation. The Zephyr adapter enters
it around remote preparation (remote-home resolution, OpenOCD version query,
helper deployment) and the session; recording mode never creates it. Nested
users borrow the active owner of their thread; they never install another
capture. The owner holds one monotonic fact, `not requested -> requested`,
which never resets during the operation, and one stable internal
`OperationCancelled` identity. `OperationCancelled` derives from
`BaseException` so ordinary exception handling cannot absorb it.

**Native capture and wakeup-fd ownership.** On the main thread, and only when
SIGINT currently dispatches to a Python-level handler, the owner installs a
non-raising handler and `signal.set_wakeup_fd()` on a dedicated nonblocking pipe.
An ignored or native-default disposition is preserved without capture, and a
worker-thread operation has no runner-owned Ctrl-C. The handler only sets the
fact: it cleans up nothing, takes no application lock, calls no authority or
user callback, logs nothing, and allocates nothing of note. Native delivery to
any thread writes a byte to the wakeup pipe before the Python handler runs on
the main thread, so a main-thread wait wakes even while that handler is
pending. Installation and restoration are ownership transitions. Installation
records restoration responsibility before each native mutation and installs
the non-raising handler before the wakeup slot, so Ctrl-C delivered to the
previous handler cannot interrupt the later steps. Restoration inspects the
process-wide wakeup slot rather than trusting a flag: it returns the slot to its
previous value only while the operation's descriptor holds it, then restores
the previous handler, and closes the pipe only after the slot no longer refers
to it; a failed slot restoration retains the descriptor. If an interruption
lost the previous slot value during installation, the slot is left empty
rather than referring to a closed descriptor. The supported west
invocation owns the process-wide wakeup slot for the live operation.
Transparent composition with an unrelated wakeup-fd owner, such as an
embedding event loop, is not supported; a previously installed descriptor is
restored afterwards but receives no wakeups meanwhile.

**Durable fact versus notification hints.** Wakeup bytes are hints only. Their
values are never decoded and a full or coalesced pipe cannot lose the fact,
which is read only from the latch. Draining a hint is safe because of one
CPython implementation property rather than a documented language guarantee:
its C-level handler records the pending signal before writing the wakeup byte,
so when a wait wakes on that byte the Python-level handler is already pending
and runs at the main thread's next signal check, before the wait re-reads the
latch. The worker-thread delivery test qualifies this on every supported
CPython version.

**Wait dispatcher and notifier ordering.** `wait_for()` is the single wait for
active operation work on the operation's thread. It composes the native
wakeup, coalescing `Notifier` hints, readable and writable descriptors
(reported separately), pidfd process completion, and an absolute deadline. It
dispatches a requested cancellation before blocking and again after consuming
wakeup hints, so cancellation takes precedence over coincident readiness.
Producers publish durable state first and then notify; consumers drain hints
only after waking and then re-read durable state, so a notification issued
after a consumer's snapshot remains pending for its next wait. Real deadlines
remain where they are semantic: helper startup, command timeouts, forwarding
readiness, RTT connection retry and initial-output grace, and cleanup budgets.
Callers never decide expiry ahead of the wait: helper startup, the foreground
session wait, and the RTT retry pass even an already expired deadline to
`wait_for()` and act on its `expired` result afterwards. A cancellation latched
by then is dispatched before any timeout, and session facts recorded before
that wait returned are re-read before expiry is reported. A cancellation
latched after the expiring wait returned is retained behind the timeout.

**Process completion and reaping.** Reaping happens only through the owner's
`Popen` on the coordinating thread. For finite commands and forwarding waits on
that same thread, the wait opens transient pidfds and closes them before
returning; no concurrent reaper can release their numeric identity at registration.
The separate helper reader instead borrows a stable pidfd from a resource-free
`ProcessCompletion` already held by `HelperConnection`. Transport registration
populates that owner before reader launch and before coordinator reaping, so
shutdown may reap without redirecting the reader to a reused PID. Cleanup closes
the descriptor only after reader settlement and independently of transport
cleanup failures; an unsettled reader retains its owned completion descriptor.
A child that exited before registration is reported immediately.
Without pidfd support (kernels before Linux 5.3, or a sandbox that denies
`pidfd_open`), a non-reaping `waitid(WNOWAIT)` re-check every 50 ms bounds exit
observation. The fallback also re-reads the original process handle's durable
returncode, including when the coordinator has already reaped. That interval
is a process-completion fallback, not cancellation
polling: cancellation, notifications, and descriptor readiness keep their own
wake sources. It deliberately replaces a sole-reaper observer thread, which
would add a thread to own without changing the single-reaper property.
Process exit and pipe EOF are observed independently; neither is inferred from
the other.

**Session authority separation.** `SessionAuthority` stays session-scoped. It
publishes each state change through an injected notifier callback and knows
nothing of signals. Pre-session failures propagate through normal operation
control flow. The single helper reader and every other session producer record
facts into the authority; the coordinator re-reads it after each wake.

**Effect-admission boundary.** Each new effect has one final admission point:
the read of the operation's cancellation fact by `admit_effect()`, evaluated as
the effect's last refusal condition. Process launches (`SshCommand.popen()` and
finite commands) perform it immediately before creating their resource-free
owner. Session-dependent work (forwarding transactions and local clients) passes
`admit_effect` to `SessionAuthority.admit_work()`, which calls it while holding
the authority lock, after the recorded-failure, closure, terminal, readiness,
and forwarding conditions, and immediately before the admission gate commits
entry. Staging and START precede readiness and occur only during acquisition,
before local closure can begin; they pass `admit_effect` to
`admit_helper_effect()`, which calls it under the same lock after only the
recorded-failure, pending control-observation, terminal, and control-end
conditions, without the admission gate. Session facts are recorded only under
that lock, so they cannot change between those checks and the read; the read
is therefore the single linearization point for both kinds of fact. A Ctrl-C latched before it refuses the effect. One latched after it
lets the admitted effect complete into its established owner, and the next
wait or admission dispatches it; a session fact recorded after it likewise
does not revoke the admitted transaction. Each transport launched by an
admitted forwarding transaction still passes its own launch admission. There
is no general checkpoint API, and local CPU or filesystem work such as
staging-archive generation is not interrupted: a cancellation recorded during
it is retained and acted upon at the next admission or wait.

**Cancellation boundaries and precedence.** Cancellation becomes control flow
only in the main-thread wait dispatcher, at final effect admission, at the
transition into session closure, and at the outer operation boundary. Its
recognition point is that dispatch or admission. Recognized cancellation is
recorded in the session authority like any other local operation fact, so a
session failure recorded earlier remains primary and keeps cancellation as a
diagnostic, while a later failure becomes a diagnostic of the cancellation.
At the outer boundary, after settlement and capture restoration, a primary
cancellation is projected to `KeyboardInterrupt`; because west reports
`KeyboardInterrupt` only through its exit status, retained diagnostics are
logged first. A non-cancellation primary failure propagates with a note that
cancellation was also requested.

**Settlement mode.** Session closure enters settlement once. If cancellation
initiated closure, the session records it and reports one status line that
remote cleanup is in progress. Native capture stays active and repeated Ctrl-C
stays recorded, but no wait or admission dispatches it again; every applicable
independent cleanup runs with its existing finite budget, and cleanup failures
are recorded without replacing an earlier primary failure. Cleanup waits are
bounded blocking waits that are not interruptible by the captured signal.

**Interactive ownership scope.** Debug and attach launch the configured client
inside `interactive_client()`. SIGINT ownership transfers to the client at one
instruction: the scope marks the client as owner, after which the runner
handler no longer latches, however late the runtime dispatches a delivery to
it. The scope then switches SIGINT to `SIG_IGN`, re-reads the cancellation
fact, and session admission is revalidated before launch. A Ctrl-C the runner
latched before the transfer refuses the launch; any later delivery belongs to
the client. This rule does not depend on how CPython orders pending handlers
around a disposition change. The client interval spans the whole configured
invocation, including wrappers before the real GDB, because no generic
readiness point exists for arbitrary wrappers. Wrappers and GDB inherit the
ignored disposition until they install their own handling, as under stock
Zephyr, and while it is ignored the kernel discards SIGINT for the runner. Exit
restores the non-raising handler, then returns ownership to the runner and
re-reads cancellation before any further runner work; delayed wakeup bytes are
never decoded into retrospective cancellation. RTT batch GDB setup is not an
interactive interval.

**Retained SSH mask.** The only remaining SIGINT mask is the long-lived SSH
launch mask of [§38.1](#381-local-ssh-subprocess-ownership). Together with the
ignored disposition set by its exec launcher, it is child signal topology that
keeps transports alive across terminal Ctrl-C intended for GDB.

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
failures, with the actual workspace and allocated address. Only explicit
runner-owned templates receive workspace or
address values; all other arguments remain literal, and templates cannot target
the configured prefix.

The pre-spawn event's wire ordering and full-contract compatibility
requirements are defined in [protocol.md](protocol.md). The architectural
startup failure and attempt policy are defined in §38; the protocol document
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
session acquisition when their runner port options are enabled.
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
Retained decoder/marker evidence and already-read but undecoded chunks remain
on the normal observation path. Each owned child stream receives the finite
nonblocking checkpoint scan defined in [section 38.4](#384-child-physical-ownership),
including its raw read budget and treatment of previously read chunks.
Account EOF if observed within that scan, flush decoder state for actual EOF,
and check current child exit without a blocking wait. The same reader owns
ordinary observation and the final scan, avoiding a competing final reader.
The checkpoint budget is not replenished by new output;
a noisy producer cannot extend the scan indefinitely. This policy may
include bytes arriving during the scan and may exclude bytes beyond the finite
cut; it is not a global timestamp guarantee.

Complete marker evidence at that boundary can satisfy readiness, but a dead
child cannot authorize live READY. Missing evidence commits startup failure
unless the authority has already committed termination. EOF and the deadline
may race with either initiating cause. The final scan observes owned child
streams and accounts recorded fatal physical adapter failures. Controller input
is observed independently. Exact complete-line/EOF
marker semantics belong in [protocol.md](protocol.md).

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
startup-marker lines when making readiness decisions. Output diagnostics cannot
authorize another child attempt. Relayed output is also represented through the
protocol's
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
Local staging preserves an exception from archive construction or transfer,
including dispatched cancellation, while closing its owned archive. A close
failure and its existing notes are attached before the initiating exception
reaches the session authority; a close failure without an earlier exception
remains fatal.
Each helper input observer scopes its descriptor mode restoration with its
observation. A read or protocol exception stays primary when restoration also
fails; the restoration exception and its existing notes are attached before the
guard records a structured diagnostic. If observation completed normally, or
was cancelled solely to settle an observer, restoration failure is reported to
the coordinator as a failure in its own right. Routine observer cancellation
does not suppress failed restoration or become an operation failure by itself.
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

The package boundaries express execution location and logical ownership:

- `controller` runs locally. `SessionController` coordinates staging, the
  `HelperConnection`, and `ForwardManager`; its synchronized `SessionAuthority`
  selects failures and admits dependent work. SSH transports, local RTT sockets,
  local archives, and the preferred-address cache remain controller resources.
  The operation cancellation owner and its wait engine own runner Ctrl-C
  capture and every active local wait ([§39.1](#391-operation-cancellation-and-waits)).
- `helper` runs on the remote host. `HelperSession` coordinates physical facts
  through its pure `LifecycleAuthority`. Workspace leases, address leases,
  `SupervisedProcess` acquisition and disposal, descriptor modes, output relays,
  and signal handlers have explicit physical owners. The CLI only dispatches
  commands and reports JSON errors; it does not implement another lifecycle.
- `protocol` is shared by both machines. It defines immutable outcomes,
  `ProcessSpec`, `ListenerSpec`, explicit argument templates, strict START
  decoding, bounded framing, event ordering, and standalone response validation.
  Canonical Tcl-word quoting belongs to this serialized template contract. These
  modules depend only on the standard library, never on OpenOCD planning, local
  transport, configuration loading, or Zephyr.
- `openocd` contains pure command planning and debug/flash operation plans. It
  depends on protocol templates and consumes controller path and forwarding
  descriptions as offline inputs without acquiring transports or live session
  resources.
- `zephyr44` adapts the supported Zephyr 4.4 runner state to board-independent
  plans; `config.py` loads and resolves the configuration contract. The west
  runner entry point remains the supported public integration boundary.

Specifications and observations do not own live resources. `SessionPlan`
contains local staging inputs and forward specifications; `ProcessSpec` describes
future remote execution. `StartRequest` represents decoded Protocol v1 input
and retains its wire validation boundary rather than duplicating controller
transport state. `SessionDescriptor` and `TerminalSnapshot` are observations.
Shared template types and the Tcl quoting rule have one canonical implementation
used during offline preview and remote materialization.

The deployment builder includes every canonical `helper` and `protocol` Python
module and the minimal executable entry point. Fixed zip metadata makes the
content-addressed digest cover their source bytes. The bundle includes no OpenOCD
planning, controller, configuration, or Zephyr dependency and executes under
isolated Python without a checkout or installed package.

Diagnostics are produced at the subsystem boundary observing each failure and
composed by the existing authority. Platform-specific SSH behavior remains in
the configured SSH command boundary.

Python naming distinguishes supported integration, internal collaboration, and
private implementation. The supported `zephyr44.runner` facade exports only
`RemoteOpenOcdBinaryRunner`; adapter operations live in `zephyr44._runner`.
Package initializers deliberately do not collect implementation exports.
`controller.api` is a separate, explicitly package-internal runtime facade for
session consumers: it groups session construction, results, errors, and advisory
callbacks across the decomposed owners. It imports their definitions directly,
without adding ownership or behavior. Controller collaborators continue to
depend on their specific modules, never back on the facade. Offline planners
retain direct imports of lightweight path, forwarding, staging, and session
descriptions, avoiding a dependency on runtime coordination.

The usage audit does not justify facades for the other implementation packages.
OpenOCD debug and flash planning already have coherent operation entry points;
their shared command and argument modules express more specific dependencies.
Protocol command, response, process, listener, argument, outcome, and framing
modules each name a distinct part of the wire vocabulary. Helper modules express
coordinator, lifecycle, observation, and physical-owner dependencies, with no
ordinary controller consumers. Aggregating those names would obscure boundaries
and broaden initialization without decoupling real callers. In particular, root,
helper, and protocol initialization stays independent of controller, configuration,
OpenOCD planning, and Zephyr; the deployed bundle remains standard-library-only.

Controller transport, process, cleanup, forwarding, deployment, and RTT
implementation modules use an `_` module prefix. The helper's physical owners,
observations, streams, output decoding, and cleanup use the same convention.
Shared names within these modules are unprefixed; `_name` remains local to its
class or defining module. Domain and protocol modules remain internal Python
interfaces even when their names are unprefixed; the Protocol v1 wire contract
is independently supported. A facade does not change that compatibility policy.
See [contributor naming guidance](../../CONTRIBUTING.md#python-api-and-encapsulation)
and [canonical import rules](../../CONTRIBUTING.md#canonical-imports) for choosing
between the runtime facade and a defining-module dependency and for deciding
whether a new concept merits an export.

These naming boundaries do not transfer resource ownership. Raw process
registration and rollback still belong to their original owner, helper control
uses the session's explicit `release_workspace()` and
`account_recorded_failures()` operations, and the stream context remains owned
and restored by that session. No acquisition, adoption, publication, settlement,
or failure-precedence boundary changes with a module rename.

### 43.1 Architecture maintenance invariants

Changes to lifecycle, concurrency, cleanup, transport, or resource ownership
shall preserve these invariants:

- Each lifecycle has one transition authority. Observers, signal handlers, reader
  tasks, and physical adapters report facts; they do not independently choose
  semantic lifecycle transitions.
- The local SIGINT handler only latches the operation's cancellation fact.
  Cancellation becomes control flow only at the wait dispatcher, effect
  admission, closure, and the outer operation boundary; ownership handoffs and
  cleanup are never interrupted by it, and no mask substitutes for ownership.
- Every acquired resource has a cleanup owner continuously from successful
  acquisition through confirmed disposal or explicit residual responsibility.
  Acquisition success, ownership adoption, publication, cancellation, and
  settlement are distinct events.
- When interruption safety is required, callers own resource-free coordinators
  before acquisition begins and enter cleanup scopes before calling acquisition.
  Acquisition populates that existing owner; it does not transfer a newly live
  coordinator or connected socket through a return value. Protect raw physical
  acquisition and registration together inside the already-owned scope.
- Timeout, cancellation, requested termination, signal delivery, and an
  unsuccessful wait do not establish physical settlement.
- Remote READY is a remote fact, not local authorization to launch dependent
  work. Local dependent work enters through the local session authority
  immediately before its effect begins.
- Local cancellation does not prove remote termination. Local and remote
  authorities act on their own observations and retain uncertainty when the
  other side cannot be observed.
- An OpenOCD child result originates only from genuine child observation.
  Helper, SSH, forwarding, timeout, protocol, and cleanup statuses never become
  OpenOCD statuses.
- Once a primary failure is established, later failures remain secondary
  diagnostics. Cleanup attempts all independently owned resources even when an
  earlier cleanup step fails.
- An effect may be retried only when repeatability is established before the
  effect executes and previous ownership is settled. A diagnostic observed
  after partial execution is not evidence that replay is safe.
- Physical adapters may retain state needed for ownership, idempotent effects,
  and physical observations. They shall not duplicate or independently decide
  semantic lifecycle state owned by the lifecycle authority.

A design warrants reconsideration before implementation if it introduces:

- a second flag or state value mirroring semantic states such as ready, failed,
  terminating, or ended outside the lifecycle authority;
- resource creation followed by later assignment to its cleanup owner, leaving
  an acquisition-to-adoption interval without rollback ownership;
- dependent work that bypasses local session entry;
- success publication before already-recognized fatal facts are accounted;
- timeout or cancellation as evidence of resource disposal;
- independent exception-precedence logic in another subsystem;
- retry justified by inspecting an error after the effect has already run;
- an observer that directly chooses readiness, termination, or final outcome; or
- an acknowledgement or fence whose purpose is to prove another observer has
  seen all earlier events.

### 43.2 Extension recipes

The following patterns are the default way to extend lifecycle-sensitive code.
A different pattern requires an architectural reason and corresponding SAD
update.

#### Add a remote resource

Use continuous ownership around the physical effect:

```text
authorize effect
    -> establish a resource-free owner and its cleanup scope
    -> acquire into that owner under interruption-safe registration
    -> validate or initialize
    -> transfer ownership explicitly
    -> normal owner retains cleanup reachability
    -> establish physical settlement independently
    -> report the resulting fact to the lifecycle authority
```

Review interruption and `BaseException` immediately before and after each
ownership handoff. The resource must remain reachable by some cleanup owner at
both points. A factory's guarded return is insufficient: interruption can occur
after the callee gives up control and before caller adoption. Keep acquisition
on an already-owned coordinator rather than returning a newly live resource.
Local SSH transport callers supply the existing `SshCommand.popen()`
`on_acquired` callback to register caller ownership inside transport rollback
before returning; caller assignment is not its sole cleanup reference.

#### Add an asynchronous observation

Keep observation separate from semantic authority:

```text
physical observer
    -> record or enqueue fact
    -> coordinator receives fact
    -> lifecycle authority assigns semantic meaning
```

The observer must not independently publish READY, select the terminal result,
or choose the primary failure.

#### Add local dependent work

Use the same entry boundary as forwarding, GDB, and RTT:

```text
check relevant physical health
    -> account already-recorded fatal facts
    -> enter through the local session authority
    -> immediately begin the effect
```

Authorization separated from actual execution by scheduling, blocking, or
another asynchronous boundary must be revalidated at execution entry.

#### Add a blocking wait

Active local work that waits on remote, network, or process activity uses the
common operation wait:

```text
snapshot durable state
    -> act on it, or wait_for(descriptors, notifiers, processes, deadline)
    -> wakeup: hints consumed, cancellation dispatched first
    -> re-snapshot durable state
```

Do not slice a wait with short timeouts to look for cancellation, and do not
add cancellation checks to business code. Keep real semantic deadlines. A
producer on another thread publishes durable state and then notifies.
Cleanup waits stay finite and are not dispatch points.

#### Add a failure source

Feed the failure into the existing composition path:

```text
observe failure
    -> preserve an already-established primary
    -> retain the new failure as ordered diagnostic information
```

Do not add independent precedence rules merely because the new failure belongs
to another subsystem. If the existing session authority cannot express the new
failure domain cleanly, reconsider or extract the local failure arbiter instead
of adding another ad hoc precedence branch.

#### Add retry behavior

Before implementing retry, document:

1. the effect that would be repeated;
2. side effects that may have occurred before failure;
3. evidence available before first execution that proves repetition safe; and
4. settlement required before another attempt.

Do not infer repeatability from a later error message or cleanup result.

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
Generic `ProcessSpec`, helper/session boundaries, and socket-forwarding
interfaces provide deterministic seams for fake OpenOCD process and socket
endpoints, so remote-session and forwarding behavior can be exercised without
physical hardware.

---

## 45. Architecture Decisions

Selected architecture:

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
- one concrete local `SessionController` owner;
- resource-free session preparation followed by single-use instance acquisition;
- no generic `SessionBackend`/`BackendSession` layer;
- cleanup-only `SessionController.close()`;
- genuine child result stored separately from shutdown trigger and infrastructure status;
- primary-failure selection by the active local operation;
- condition-driven bounded local shutdown after controller-input half-close;
- one remote phase-specific lifecycle authority with a single child attempt;
- local launch gate, revalidated at actual execution entry;
- structured outcome and one immutable terminal snapshot;
- finite child-stream final observation at readiness determination;
- event-driven local waits: descriptor readiness, state notifications, and
  pidfd process completion instead of health polling;
- operation-owned, non-raising runner Ctrl-C capture dispatched only at defined
  boundaries, with interactive GDB owning Ctrl-C for its whole invocation;
- no persistent cross-session firmware or configuration cache;
- local and remote cleanup are bounded after their respective loss
  observations; detection timing and the interval between observations are
  delegated to the SSH and operating-system layers.

---

## 46. Architectural tradeoffs and complexity

| Concern | Architectural decision and cost |
| --- | --- |
| Controller lifetime | Open controller stdin is the remote lease. RELEASE then directional EOF ends it intentionally while reverse output remains available; EOF recognized without RELEASE is lease loss, retained at recognition without a fence or acknowledgement. Configured SSH wrappers require qualification at this boundary. |
| Separate authorities | The local launch gate owns dependent launch eligibility; the remote authority owns attempts, readiness, termination, and terminal freeze. Each acts on its own observations. |
| Continuous physical ownership | Acquisition and adoption must preserve cleanup reachability across interruption. Process groups, producers, staging exclusion, forwards, and RTT resources require independent settlement evidence. |
| Bounded observation and reporting | Finite final observation, bounded frame and writer storage, and a finite terminal drain keep failure handling bounded. One structured snapshot preserves result provenance and nested diagnostics. |
| Startup and residual storage | One child creation attempt avoids repeating opaque/user effects after startup failure. Phase checks reject invalid late observations. Residual-child markers and isolated storage formats prevent uncertain cleanup from authorizing unsafe reclamation. |

The local session authority gates new forwarding work and public GDB/RTT launch. The remote
coordinator accounts already-recorded observer and signal failures before
publication.
The [lifecycle traceability](../traceability/verification.md#controller-lease-lifecycle)
records maintained evidence and acceptance boundaries.

The bounded writer owns partial writes and a 16-MiB queue. Each direction uses
the canonical one-MiB frame codec; incremental rendering rejects excessive
output before accumulating an unbounded encoded frame. Terminal delivery
receives a finite drain opportunity after physical cleanup. Delivery failure
records local diagnostics and nonzero helper status without another freeze.

Local shutdown cancels launch first, writes RELEASE if still needed, half-closes
stdin, retains the event reader
through a 15-second coordination window, then applies bounded cleanup to the
owned SSH transport and reader. Missing or malformed terminal information and
independent nonzero helper/SSH status are failures. The shared connection master
is externally owned and is never a cleanup target. Native helper signals are
failures, including signals after freeze; child disposal signals retain their
separate result provenance.

The raw reader's sole-reader wake mechanism and finite child-stream checkpoint
support final readiness observation. Control reader cleanup stays in the
observer's `finally` block, and its task is owned by the session task group and
cancellation ledger.

Already-recorded fatal observation accounting, native-signal commit boundaries,
physical settlement evidence, and local primary-failure arbitration require
regression coverage at their production boundaries. External qualification
boundaries and remaining limits are described in
[verification.md](../traceability/verification.md#integration-qualification).
