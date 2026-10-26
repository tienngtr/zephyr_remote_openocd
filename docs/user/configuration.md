# Configuration

The `remote_openocd` runner reads one YAML configuration file that defines the
remote hosts and how to reach them. By default the file is
`~/.config/zephyr_remote_openocd/config.yaml`. This page covers creating and
validating the file, defining remotes, and the rules the runner applies to
each setting.

## Quick start

In the commands below, replace `/path/to/zephyr_remote_openocd` with the path
where the module was placed.

1. Create the configuration file with the
   [setup script](../../README.md#2-run-the-setup-script), using the Python
   environment configured for Zephyr 4.4.x:

   ```sh
   python3 /path/to/zephyr_remote_openocd/scripts/user/setup.py
   ```

   The setup script creates `~/.config/zephyr_remote_openocd/config.yaml` and
   never overwrites an existing file.

   To create the file at another location, pass its path as the script's
   argument, then set `ZEPHYR_REMOTE_OPENOCD_CONFIG` as described in
   [Location](#location) so that the build and `remote_openocd` commands read
   that file.

   The generated file contains a `lab` remote that is safe to keep. Nothing
   uses it until `--remote lab`, `ZEPHYR_REMOTE_OPENOCD_REMOTE=lab`, or
   `default_remote: lab` selects it.

2. In the `lab` remote, replace the `ssh_host` value with the remote host name
   or an alias from the normal SSH configuration, and replace the
   `openocd_command` path with the path of the OpenOCD executable on that
   remote host:

   ```yaml
   remotes:
     lab:
       ssh_host: replace-with-ssh-host-or-alias
       openocd_command:
         - /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd
   ```

3. [Validate the configuration](#validate-the-configuration) without
   contacting SSH, OpenOCD, GDB, or hardware:

   ```sh
   python3 /path/to/zephyr_remote_openocd/scripts/user/validate_configuration.py \
     --remote lab
   ```

   For a configuration file at another path, put that path before `--remote`,
   as in the validator command that the setup script printed.

4. Check the remote prerequisites that validation does not cover: that the
   configured SSH command reaches the host, that the remote `python3` is
   version 3.12 or newer, and that the configured OpenOCD executable runs
   there. The commands are in the README's
   [validation step](../../README.md#5-validate-the-configuration-and-remote-prerequisites).

5. Use the remote explicitly. This requires the module to be
   [activated](../../README.md#3-activate-the-module) and the build
   [configured](../../README.md#6-configure-the-build) so that
   `west flash --context` lists `remote_openocd`:

   ```sh
   west flash -r remote_openocd --remote lab
   ```

### Choose the default remote and runner

For an OpenOCD-capable build, one whose board supports Zephyr's `openocd`
runner, activating this module replaces the board's
default flash and debug runner when CMake configures the build.
`default_runner` chooses the replacement; omitting the key selects `openocd`.

Keep `default_runner: openocd` to use local OpenOCD by default, or change it to
`remote_openocd`. Other runners remain available with `-r`. The build stores
this default; see
[Apply configuration changes to a build](#apply-configuration-changes-to-a-build)
for when a change takes effect.

After the explicit command works, uncomment `default_remote: lab` to omit
`--remote lab`; see [Remote selection order](#remote-selection-order).

## Configuration file

### Location

The build and `remote_openocd` commands read
`~/.config/zephyr_remote_openocd/config.yaml`. To use another file, set
`ZEPHYR_REMOTE_OPENOCD_CONFIG` to its absolute path, without `..` components,
before configuring the build and in every shell that runs west. An unset or
empty variable selects the default path. See
[Configuration path rules](#configuration-path-rules) for the accepted values.

The example below creates and selects `$HOME/lab/config.yaml`. To use another
file, replace `$HOME/lab/config.yaml` with that file's absolute path, and
replace `$HOME/lab` in the `mkdir` command with its parent directory. The
`mkdir` command creates the parent directory, which the setup script requires
for a file other than the default.

```sh
mkdir -p "$HOME/lab"
python3 /path/to/zephyr_remote_openocd/scripts/user/setup.py "$HOME/lab/config.yaml"
export ZEPHYR_REMOTE_OPENOCD_CONFIG="$HOME/lab/config.yaml"
python3 /path/to/zephyr_remote_openocd/scripts/user/validate_configuration.py \
  "$ZEPHYR_REMOTE_OPENOCD_CONFIG" --remote lab
```

If the setup argument is relative, starts with `~/`, or contains `..`, run the
`export ZEPHYR_REMOTE_OPENOCD_CONFIG=...` line that the setup script prints
instead of writing the assignment by hand; the printed path is absolute and
has no `..` components.

After changing the variable for an existing build,
[reconfigure CMake](#apply-configuration-changes-to-a-build) so that the build's
stored default runner comes from the new file.

To set the variable in shells that source Zephyr's `zephyr-env.sh`, add the
`export` line to `~/.zephyrrc`, as the README's
[Activate the module](../../README.md#3-activate-the-module) step does for
`EXTRA_ZEPHYR_MODULES`.

### Apply configuration changes to a build

The build stores only the default runner derived from `default_runner`.
`remote_openocd` commands read the rest of the configuration each time they run.

Once this module is configured into a build, ordinary `west flash` and
`west debug` commands run an incremental build before selecting the runner.
That build updates the stored default after any of these changes to the
configuration file selected when CMake last configured the build:

- its content is edited;
- it is created or deleted;
- another file is moved or copied into place, even one with an older
  timestamp;
- a symbolic link in its path is switched to another file or directory.

Run `west build` to apply these changes without flashing or debugging.
Applying configuration changes requires neither a pristine build nor a full
firmware compile and link.

**Generators other than Ninja.** A build directory generated for another
generator, such as Unix Makefiles, instead of Zephyr's default generator,
Ninja, applies edits, creation, and deletion in the same incremental build. A
moved or copied file or a switched symbolic link whose timestamp is not newer
than the build's generated files is detected but not applied in that
invocation. The incremental build stops with an error before flashing or
debugging:

```text
Zephyr Remote OpenOCD configuration PATH changed after CMake last configured this build, so its stored default runner may be stale.
```

Run the same `west flash`, `west debug`, or `west build` command again. Its
incremental build reconfigures CMake and updates the stored default runner
before continuing.

A `--no-rebuild` runner command skips this refresh and uses the stored default
as it is.

Replace `<build>` with the existing Zephyr build directory. To select the
configuration file again and refresh the stored default runner, run
`west build --cmake-only -d <build>` when:

- the build directory predates activation of this module;
- `ZEPHYR_REMOTE_OPENOCD_CONFIG` selects a different file. An ordinary
  incremental build does not detect an environment-only path change. Until
  CMake is reconfigured, the stored default can still come from the previous
  file while `remote_openocd` commands read the new one.

### CMake configuration errors

> [!IMPORTANT]
> CMake configuration stops with an error for an OpenOCD-capable build with
> this module active, even with `default_runner: openocd`, if any of these
> conditions applies:
>
> - `ZEPHYR_REMOTE_OPENOCD_CONFIG` is non-empty but is not absolute or contains
>   a `..` path component;
> - PyYAML or jsonschema is unavailable in the Python environment that CMake
>   uses;
> - the selected path exists but cannot be read as a file, such as a dangling
>   symbolic link, or the file is not valid UTF-8;
> - the file contains malformed YAML;
> - the file has an invalid document structure, such as multiple YAML documents;
> - the file contains duplicate mapping keys, including inside remote settings;
> - the file contains unknown top-level keys, such as `default_runnner`;
> - `default_runner` has an invalid type or value.
>
> CMake prints the error and does not substitute local OpenOCD or a previous
> valid default. For a rejected path, see
> [The configuration path is rejected](troubleshooting.md#the-configuration-path-is-rejected).
> Run the [validator](#validate-the-configuration) to find
> errors in the file; it does not check the environment variable's path rules.

See [Apply configuration changes to a build](#apply-configuration-changes-to-a-build)
for when changes cause CMake to rerun and when manual reconfiguration is needed.

CMake checks only what it needs to choose the default runner; success does not
mean the remote settings are valid. After YAML parsing succeeds, invalid
`default_remote`, `presets`, or `remotes` values do not stop CMake when
`default_runner` selects either `openocd` or `remote_openocd`.

Run the [validator](#validate-the-configuration) to check the complete file
before using the remote settings. A `remote_openocd` command also checks the
whole file before contacting the remote host.

### Format

The file is read as YAML regardless of its filename extension.

[`configuration.schema.json`](../../python/zephyr_remote_openocd/resources/configuration.schema.json)
defines every key, its allowed values, and their lexical forms;
[`config.example.yaml`](../../resources/config.example.yaml) is a commented
template.

- An empty YAML document is equivalent to `{}`.
- A `remote_openocd` command and the validator reject unknown keys, duplicate
  YAML mapping keys, null values, and type mismatches. See
  [CMake configuration errors](#cmake-configuration-errors) for CMake's checks.
- A missing configuration file uses the defaults. The validator is stricter;
  see [Validate the configuration](#validate-the-configuration).
- A dangling symbolic link or another existing path that cannot be read as a
  file is an error. Valid symbolic links to configuration files are supported.

### Editor assistance

The JSON Schema provides hover text, defaults, examples, completion choices,
and unknown-key validation. With the Red Hat YAML extension for VS Code, add an
association like this to user settings:

```json
{
  "yaml.schemas": {
    "/path/to/zephyr_remote_openocd/python/zephyr_remote_openocd/resources/configuration.schema.json": [
      "/home/USER/.config/zephyr_remote_openocd/config.yaml"
    ]
  }
}
```

Replace `/path/to/zephyr_remote_openocd` with the absolute path of the module,
and `/home/USER/.config/zephyr_remote_openocd/config.yaml` with the absolute
path of the configuration file.

## Validate the configuration

```sh
python3 /path/to/zephyr_remote_openocd/scripts/user/validate_configuration.py \
  [CONFIG] [--remote NAME]
```

Replace `/path/to/zephyr_remote_openocd` with the module path, as in the
[Quick start](#quick-start).

- `CONFIG` is the configuration file path. Omit it to read
  `~/.config/zephyr_remote_openocd/config.yaml`.
- `--remote NAME` selects the remote to check.

The validator ignores `ZEPHYR_REMOTE_OPENOCD_CONFIG`. When that variable is
non-empty, pass `"$ZEPHYR_REMOTE_OPENOCD_CONFIG"` as `CONFIG` to check the file
that `remote_openocd` commands read. The validator checks the file's contents;
it does not check whether the variable meets the
[path rules](#configuration-path-rules).

Validation checks:

- **The whole file:** syntax and schema rules, and the local paths of every
  path mapping, which are resolved and checked for collisions.
- **The selected remote:** its preset reference and, after applying its
  defaults, its required settings.
- **Not checked:** broken references or missing operational settings in
  unselected remotes, SSH connectivity, and whether the configured executable
  exists.

The validator selects the remote from `--remote`, then `default_remote`.
It ignores `ZEPHYR_REMOTE_OPENOCD_REMOTE`; when that variable is non-empty,
pass `--remote "$ZEPHYR_REMOTE_OPENOCD_REMOTE"` to check the remote that
`remote_openocd` would select. With no remote selected, the validator reports
that the file is structurally valid and lists the available definitions.

For a selected remote, the validator prints the effective settings without
redaction: SSH and OpenOCD argv, hosts, path mappings, and resolved paths. It
lists `forward_env` names but does not collect or display their current
values. It still reads variables independently needed for configuration
discovery and path resolution, even when those names also appear in
`forward_env`.

The validator requires its target file to exist. It does not treat a missing
file as an empty configuration.

For a missing file, the error suggests a setup command for that path. If a
custom path's parent directory is also missing, the error names the directory
to create before running the setup script.

## Remotes

A minimal usable remote needs only a name and an OpenOCD command:

```yaml
remotes:
  lab:
    openocd_command: [/path/to/openocd]
```

Select it with `--remote lab`. Because the remote is named `lab`, omitting its
other fields gives these effective settings, so the SSH client must resolve
`lab`:

```yaml
ssh_host: lab
ssh_command: [ssh]
forward_env: []
path_mappings: {}
```

A preset holds settings that several remotes share; see
[Defaults and inheritance](#defaults-and-inheritance). This example shows
default selection and preset reuse:

```yaml
default_runner: openocd
default_remote: lab

presets:
  default:
    openocd_command:
      - ~/openocd/bin/openocd
    ssh_command: [ssh]
    forward_env: [FTDI_CHANNEL]
    path_mappings:
      ~/zephyr-sdk/openocd/scripts: ~/zephyr-sdk/openocd/scripts

remotes:
  lab:
    preset: default
    ssh_host: lab_alias
```

### What runs where

`ssh_command` is an argv sequence executed on the local machine to start SSH.
`openocd_command` is an argv sequence sent through SSH and executed on the
remote host. West and GDB remain local. The runner also deploys the remote
helper, a Python program that runs with the remote `python3`.

The runner deploys the helper from its own module copy. Update that copy as a
whole; the runner does not support using a helper from another revision.

### Custom SSH arguments

`ssh_command` is a local argv list. Put fixed client options before the host;
the runner appends the selected `ssh_host` and the remote command:

```yaml
remotes:
  lab:
    openocd_command: [/path/to/openocd]
    ssh_command: [custom-ssh, -F, /path/to/ssh_config]
```

The runner executes this argv directly, without a shell, so shell pipelines,
redirections, and command strings do not work. SSH authentication remains the
configured client's responsibility.

#### SSH client requirements

The configured command must:

- run a remote command while passing local stdin to it in byte order, relaying
  its stdout and stderr, and returning its exit status;
- support the [stdin half-close](#stdin-half-close);
- support local TCP forwarding with an OpenSSH-compatible `-L` option;
- honor the runner's `ExitOnForwardFailure=yes` and `ClearAllForwardings=no`
  forwarding options as OpenSSH does.

The executable need not be OpenSSH's `ssh`. A bare name is found through
`PATH`, and an explicit path is preserved. For example, a Linux environment
with an SSH client at a mounted path may use:

```yaml
ssh_command: [/mnt/c/Windows/System32/OpenSSH/ssh.exe]
```

The example does not imply that this client meets the requirements; naming an
executable only selects that program.

#### Stdin half-close

Closing local stdin must deliver EOF to the remote command while leaving
stdout and stderr readable until the remote command exits; session cleanup
depends on this. For normal cleanup the runner writes a short release message
on stdin, closes stdin, and then receives the final result. `remote_openocd`
does not support wrappers that:

- suppress stdin;
- close both directions together;
- require remote pseudo-terminal behavior that prevents the half-close;
- detach the remote helper from the controlling SSH channel.

When the remote helper recognizes that its input ended without the release
message, it reports `CONTROLLER_LEASE_LOST` and the operation fails; see
[`CONTROLLER_LEASE_LOST`](troubleshooting.md#controller_lease_lost). There is
no alternate shutdown mode or YAML option, so replace or reconfigure an
incompatible wrapper.

#### Forwarding options

The runner adds `ExitOnForwardFailure=yes` and `ClearAllForwardings=no` so that
a local service forward cannot be silently discarded and a conflicting local
bind fails that forward's startup. A required forward that fails this way fails
the operation, and a best-effort forward normally produces a warning. See
[Forwarded services](operations.md#forwarded-services).

SSH commands that create local service forwards get these options before the
configured fixed arguments. With a client that honors OpenSSH's first-value
precedence, the options take precedence over conflicting fixed arguments and
SSH configuration. The configured fixed arguments are otherwise preserved
exactly and in order, and the runner's other SSH commands use the configured
arguments unchanged.

#### Connection sharing

SSH connection sharing, including `ControlMaster`, remains managed by the SSH
client; the runner does not disable it or issue client-specific cancellation
commands.

With OpenSSH `ControlMaster auto`, `ControlPersist no`, and no existing master,
a runner-launched SSH process can itself become the master and couple other
sessions to its lifetime; see
[Other sessions disconnect when one session ends](troubleshooting.md#other-sessions-disconnect-when-one-session-ends).
For concurrent sessions over a shared connection, set `ControlPersist` to
`yes` or a time so that the master runs as a separate background process, or
establish the master outside the runner first, for example with
`ssh -MNf HOST`. Run that command with the SSH executable and fixed options
from `ssh_command`, replacing `HOST` with the remote's `ssh_host`.

Runner cleanup does not remove forwards retained by a sharing master. A
connection-sharing client may reuse an existing forward with matching endpoints.

The runner's best-effort preferred-address cache can help that reuse. It has no
YAML setting. Stale or unreadable cache data does not by itself fail an operation
or bypass the remote helper's checks that no other session holds the address
and that its remote ports are available. See
[Preferred-address cache](troubleshooting.md#preferred-address-cache) and
[A retained forward occupies the local port](troubleshooting.md#a-retained-forward-occupies-the-local-port).

### Fixed OpenOCD arguments

Arguments after the executable in `openocd_command` are passed to remote
OpenOCD before the runner's own arguments, with their exact values and order.
They are an advanced option: the runner does not parse, classify, reorder, or
validate their OpenOCD/Tcl semantics, does not substitute text in them (see
[No placeholder substitution](#no-placeholder-substitution)), and does not
stage files they name (see
[Files copied to the remote](#files-copied-to-the-remote)).

Avoid conflicts among fixed arguments, and between fixed arguments and
runner-generated arguments; the runner does not inspect fixed arguments for
conflicts. For example,
`-c init`, `-f early.cfg`, or `-c 'source early.cfg'` can initialize OpenOCD or
create listeners before the runner's bind and service settings take effect;
avoid that unless it is intended. The runner's startup-ordering guarantees
cover its generated arguments only; behavior introduced by fixed arguments is
outside those guarantees.

Run a remote runner operation with `west -v` to see the full shell-escaped
effective remote OpenOCD argv, including fixed arguments, and runner-generated
arguments with the session workspace and address resolved:

```sh
west -v debugserver -r remote_openocd --remote lab
```

This output is not redacted, so literal sensitive values in configured
arguments appear in it. The remote helper reports each effective command before
attempting to start OpenOCD, so the trace is normally available when OpenOCD
fails to start or become ready, or when required forwarding fails, unless the
connection is lost before the trace is delivered.

The remote helper starts OpenOCD at most once per session start. If a service
fails to bind after OpenOCD has started, the runner reports the failure and
does not restart OpenOCD, because startup may already have changed target state
or run user-supplied OpenOCD commands. An address conflict found before OpenOCD
starts can instead move the session to another address without repeating that
work.

### Files copied to the remote

The runner copies (stages) the required local files and directories for each
session automatically, unless a path mapping supplies an existing remote
resource. Local Zephyr board-support and configuration directories are normally
staged, so local changes are used. A file referenced only by fixed
`openocd_command` arguments or by OpenOCD commands is not identified as a
required input, so the runner does not stage, map, or translate it because of
that reference.

Map a large, stable resource only when it already exists on the remote host,
such as an OpenOCD scripts tree. Mapped resources are not copied:

```yaml
remotes:
  lab:
    openocd_command: [/opt/openocd/bin/openocd]
    path_mappings:
      /path/to/local/openocd/scripts: /opt/openocd/share/openocd/scripts
```

The mapping key is resolved locally and its value is used remotely. The runner
assumes the remote contents are equivalent to the local ones and does not
compare or verify them. When mappings overlap, the most specific local path
applies. Unmapped required inputs continue to be staged automatically. See
[Path mappings](#path-mappings) for the path rules.

### Forwarded environment

Forward an environment variable when remote OpenOCD needs its local value, for
example in a configuration file. A typical use is selecting the channel of a
multi-channel FTDI adapter, such as an FT2232H or FT4232H, so that one
configuration file serves targets wired to different channels. Allow-list the
name in the remote definition or its preset:

```yaml
remotes:
  lab:
    openocd_command: [/opt/openocd/bin/openocd]
    forward_env: [FTDI_CHANNEL]
```

The value is available to remote OpenOCD before it processes its configuration
files. Read it in the OpenOCD configuration file with the Jim Tcl `env`
command:

```tcl
adapter driver ftdi
ftdi channel [env FTDI_CHANNEL 0]
```

Then set it locally for each command:

```sh
FTDI_CHANNEL=1 west flash -r remote_openocd --remote lab
```

- **Only set variables are sent.** `FTDI_CHANNEL` is sent only if it is set in
  the local environment. Values are never stored in the configuration.
- **A missing local variable produces a warning.** The runner omits its name
  and value from the request, but a same-named value already present in the
  remote helper environment may remain available to OpenOCD.
- **The Tcl default decides the fallback.** When no value is available,
  `[env FTDI_CHANNEL 0]` falls back to channel 0, so the file also works
  without the variable. Omit the default, as in `[env FTDI_CHANNEL]`, to make
  OpenOCD stop during configuration instead.

Forwarding suits values chosen per shell or per user. For a value that one
command line always uses, pass an ordinary runner option instead, which needs
no allow-list: `--cmd-pre-init='ftdi channel 1'` overrides the channel set by
the configuration file, because Zephyr runs pre-init commands after
configuration files and before `init`.

## Defaults and inheritance

| Key | Effect when omitted |
| --- | --- |
| `default_runner` | Use `openocd`. |
| `default_remote` | No configuration fallback selects a remote. Use `--remote NAME` or `ZEPHYR_REMOTE_OPENOCD_REMOTE`. |
| `presets` | Use an empty preset mapping. |
| `remotes` | Use an empty remote mapping; no remote operation can be selected. |
| Remote `preset` | Do not inherit a preset. |
| Remote `ssh_host` | Use the selected remote's name. |
| `openocd_command` | Inherit it from the selected preset; without one, a real remote operation fails. |
| `ssh_command` | Inherit it from the selected preset, then use `[ssh]`. |
| `forward_env` | Inherit it from the selected preset, then use `[]`. |
| `path_mappings` | Inherit it from the selected preset, then use `{}`. |

A selected remote first inherits fields from its preset; preset fields have
no independent runtime defaults. Each field set directly in the remote
definition then replaces the corresponding preset field as a whole; lists and
mappings are never merged. Finally, the defaults in the table apply to values
that remain unset. Set `forward_env: []` or `path_mappings: {}` in the remote
definition to erase an inherited collection.

Preset and remote names are case-sensitive and must match
`[A-Za-z0-9][A-Za-z0-9._-]*`.

### Remote selection order

A `remote_openocd` command selects the remote from the first of these that is set:

1. `--remote NAME`;
2. the `ZEPHYR_REMOTE_OPENOCD_REMOTE` environment variable, when non-empty (an
   empty value is treated as unset);
3. `default_remote`.

A selected remote and its named preset must exist, and a real remote operation
must provide `openocd_command`; unused incomplete definitions are allowed.

## Reference

### Configuration path rules

When non-empty, `ZEPHYR_REMOTE_OPENOCD_CONFIG` must contain an absolute path
with no `..` component. This keeps the build and `remote_openocd` commands
reading the same file when they run from different directories. `.`
components and symbolic links in the path are allowed.

The rule applies to the value after shell expansion. In the shell,
`export ZEPHYR_REMOTE_OPENOCD_CONFIG="$HOME/lab/config.yaml"` stores an
absolute path. So does an unquoted assignment such as
`ZEPHYR_REMOTE_OPENOCD_CONFIG=~/lab/config.yaml` followed by
`export ZEPHYR_REMOTE_OPENOCD_CONFIG`, because the shell expands the `~` before
storing the value. Both `'~/lab/config.yaml'` and `"~/lab/config.yaml"` keep a
literal leading `~` and are rejected.

| Path stored in the variable | Result |
| --- | --- |
| `/directory/config.yaml` | Accepted |
| `/directory/./config.yaml` | Accepted; `.` leaves the directory unchanged |
| `/directory/../config.yaml` | Rejected |
| `config.yaml`, `./config.yaml`, or `../config.yaml` | Rejected |
| `~`, `~/config.yaml`, or `~user/config.yaml` | Rejected; CMake and `remote_openocd` do not expand a leading `~` |
| `/directory/~/config.yaml` or `/directory/~user/config.yaml` | Accepted; tilde names are literal |

Names such as `.config` and `config..yaml` are ordinary names, not `..`
components. For a configuration in another account's home, supply its absolute
path instead of a value beginning with `~user/`.

A rejected value stops CMake configuration or regeneration of an
OpenOCD-capable build, even when the default runner is local OpenOCD, and a
`remote_openocd` command rejects it before contacting the remote host. Other
runners do not read the variable. To fix a rejected value, see
[The configuration path is rejected](troubleshooting.md#the-configuration-path-is-rejected).

The setup script and validator take the file path as an argument and ignore
`ZEPHYR_REMOTE_OPENOCD_CONFIG`; if that variable is non-empty, pass
`"$ZEPHYR_REMOTE_OPENOCD_CONFIG"` as the argument to use the file it selects.
Their argument may be relative to the current directory or start with `~/`,
and the variable's restrictions do not apply to it.

### Commands

Commands are argv sequences, not shell strings. The first element must be a
bare executable name, an absolute path, or `~/path`; later elements are literal
strings and may be empty. NUL is forbidden in every element. The schema
enforces these lexical rules. Runner-generated OpenOCD arguments are appended
after the configured command.

Among command elements, only `ssh_command[0]` expands `~` locally. The
`openocd_command` executable and remote mapping destinations expand `~` using
the SSH user's actual home, queried once only when needed during a real
operation. The runner passes fixed arguments literally, without `~`
expansion.

### Path mappings

Path mappings are YAML mappings from local to remote paths. Both sides must be
absolute or start with `~`, and neither may contain NUL. Only the current-user
`~` form is accepted.

- **Local paths** may contain `.` and `..`. They are expanded and resolved on
  the local machine before duplicate detection. Duplicate normalized local keys
  are errors even when their remote destinations match.
- **Remote paths** must already be normalized: repeated or trailing separators
  and `.` or `..` components are rejected. They cannot be resolved safely while
  loading the configuration, because remote components may be symbolic links
  and loading does not contact the remote host.

Mappings refer to resources already present on the remote host and do not
stage local contents. Local and remote paths may contain spaces; quote them as
required by YAML syntax.

### No placeholder substitution

Mapping destinations and filenames are literal paths. `{workspace}` and
`{address}` are not configuration placeholders or reserved spellings. The
runner resolves its own session references separately, without substituting
matching text in configured paths, OpenOCD commands supplied through runner
options or board runner arguments, or fixed command arguments.

### Checks beyond the schema

`remote_openocd` commands and the [validator](#validate-the-configuration)
apply the complete schema, which enforces the lexical forms on this page.
Beyond the schema, only conditions that string syntax cannot express are checked:
local home expansion and filesystem resolution, detection of colliding
normalized local paths, selected remote and preset references, mandatory
settings for the requested operation, and remote home expansion during a real
operation.
