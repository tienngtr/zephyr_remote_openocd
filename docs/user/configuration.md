# Configuration

## Quick start

Create the canonical configuration:

```sh
python3 /path/to/zephyr_remote_openocd/scripts/setup.py
```

Setup creates `~/.config/zephyr_remote_openocd/config.yaml` and never
overwrites an existing file. The generated file contains a safe, unselected
`lab` remote. Edit it and replace these two placeholders:

```yaml
remotes:
  lab:
    ssh_host: replace-with-ssh-host-or-alias
    openocd_command:
      - /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd
```

The SSH host may be a host name or an alias from your normal SSH configuration.
The OpenOCD path is interpreted on that remote host. Validate the file and show
the effective settings without contacting SSH, OpenOCD, GDB, or hardware:

```sh
python3 /path/to/zephyr_remote_openocd/scripts/validate_configuration.py \
  --remote lab
```

Validation always checks syntax and schema rules. When a remote is selected, it
also checks that remote's preset reference, applies its defaults, and checks its
required settings. Broken references or missing operational settings in
unselected remotes remain allowed. Validation does not verify connectivity or
that the configured executable exists. Check those prerequisites separately:

```sh
ssh replace-with-ssh-host-or-alias python3 --version
ssh replace-with-ssh-host-or-alias \
  /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd \
  --version
```

Then use the remote explicitly:

```sh
west flash -r remote_openocd --remote lab
```

After that works, uncomment `default_remote: lab` if desired. Keep
`default_runner: openocd` to preserve local OpenOCD as the default, or change it
to `remote_openocd` and run `west build` to regenerate the build's runner
configuration.

Set `ZEPHYR_REMOTE_OPENOCD_CONFIG` to read a different file; its contents are
YAML regardless of its filename extension. The validator uses the same override
when no configuration path is passed. An explicit validator `--remote` takes
precedence over `default_remote`; unlike a real runner operation, validation
deliberately ignores `ZEPHYR_REMOTE_OPENOCD_REMOTE` so its result is
deterministic.

The complete machine-readable contract is
[`configuration.schema.json`](../../python/zephyr_remote_openocd/resources/configuration.schema.json), and
the commented template is [`config.example.yaml`](../../resources/config.example.yaml).
An empty YAML document is equivalent to `{}`. Unknown keys, duplicate YAML
mapping keys, null values, and type mismatches are errors.
A genuinely absent configuration file uses the defaults. A dangling symbolic
link or another existing path that cannot be read as a file is an error; valid
symbolic links to configuration files are supported.

The user-facing validator requires its target file to exist. If setup has not
created the default file, it exits with guidance instead of reporting empty
runtime defaults as a valid user configuration.

## Editor assistance

The JSON Schema provides hover text, defaults, examples, completion choices,
and unknown-key validation. With the Red Hat YAML extension for VS Code, add an
association like this to user settings, replacing both absolute paths:

```json
{
  "yaml.schemas": {
    "/path/to/zephyr_remote_openocd/python/zephyr_remote_openocd/resources/configuration.schema.json": [
      "/home/USER/.config/zephyr_remote_openocd/config.yaml"
    ]
  }
}
```

The schema is authoritative for structure and lexical rules. The validator
also resolves preset references and settings required by the selected remote.

## What runs where

`ssh_command` is an argv sequence executed on the local machine to start SSH.
`openocd_command` is an argv sequence sent through SSH and executed on the
remote machine. West and GDB remain local.

## What is copied to the remote

Required local files and directories are staged automatically unless a path
mapping supplies an existing remote resource. Local Zephyr board-support and
configuration directories are normally staged, so local changes are retained.
Use a mapping for large, stable resources already installed remotely, such as
an OpenOCD scripts tree; mapped resources are not copied. Paths with spaces are
supported when quoted as required by YAML.

## Top-level settings

A minimal usable remote needs only a name and an OpenOCD command:

```yaml
remotes:
  lab:
    openocd_command: [/path/to/openocd]
```

Select it with `--remote lab`. Because the remote is named `lab`, omitting its
other fields gives the equivalent effective settings:

```yaml
ssh_host: lab
ssh_command: [ssh]
forward_env: []
path_mappings: {}
```

The complete top-level example below shows default selection and preset reuse:

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

### Custom SSH arguments

`ssh_command` is a local argv list. Put fixed client options before the host;
the runner appends the selected `ssh_host` and remote command:

```yaml
remotes:
  lab:
    openocd_command: [/path/to/openocd]
    ssh_command: [custom-ssh, -F, /path/to/ssh_config]
```

Do not use shell pipelines, redirections, or a command string. SSH
authentication remains the configured client's responsibility.

The executable need not be named `ssh`. A bare name is found through `PATH`,
and an explicit path is preserved. For example, a Linux environment that can
execute an OpenSSH-compatible client at a mounted path may use:

```yaml
ssh_command: [/mnt/c/Windows/System32/OpenSSH/ssh.exe]
```

This is generic executable selection; it does not select another product
backend or claim compatibility with a particular client implementation.

### Installed resources and environment

Map a large resource only when it already exists on the remote. Forward an
environment variable only when the remote OpenOCD configuration needs its
local value:

```yaml
remotes:
  lab:
    openocd_command: [/opt/openocd/bin/openocd]
    forward_env: [OPENOCD_ADAPTER_SERIAL]
    path_mappings:
      /path/to/local/openocd/scripts: /opt/openocd/share/openocd/scripts
```

Here the mapping key is inspected locally, its value is used remotely, and
`OPENOCD_ADAPTER_SERIAL` is sent only if it is set in the local environment.
Unmapped required inputs continue to be staged automatically.

## Defaults and inheritance

| Key | Effect when omitted |
| --- | --- |
| `default_runner` | Use `openocd`. |
| `default_remote` | No configuration fallback selects a remote. Use `--remote NAME` or `ZEPHYR_REMOTE_OPENOCD_REMOTE`. |
| `presets` | Use an empty preset mapping. |
| `remotes` | Use an empty remote mapping; no remote operation can be selected. |
| Remote `preset` | Do not inherit a preset. |
| Remote `ssh_host` | Use the selected remote's name. |
| `openocd_command` | Inherit it from the selected preset; without one, a production remote operation fails. |
| `ssh_command` | Inherit it from the selected preset, then use `[ssh]`. |
| `forward_env` | Inherit it from the selected preset, then use `[]`. |
| `path_mappings` | Inherit it from the selected preset, then use `{}`. |

Preset fields have no independent runtime defaults. A selected remote first
inherits fields from its preset. Each field present directly on the remote then
replaces the corresponding preset field as a whole; lists and mappings are
never merged. Finally, the defaults in the table apply to values that remain
unset. Use `forward_env: []` or `path_mappings: {}` on the remote to erase an
inherited collection.

Preset and remote names are case-sensitive and must match
`[A-Za-z0-9][A-Za-z0-9._-]*`.

Remote selection uses `--remote NAME`, then the non-empty
`ZEPHYR_REMOTE_OPENOCD_REMOTE` environment variable, then `default_remote`.
An empty environment variable is treated as unset. A selected remote and its
named preset must exist, and a production operation must provide
`openocd_command`; unused incomplete definitions are allowed.

## Commands and paths

Commands are argv sequences, not shell strings. The first element must be a
bare executable name, an absolute path, or `~/path`; later elements are literal
strings and may be empty. NUL is forbidden in every element. These lexical
rules are enforced by the schema. Runner-generated OpenOCD arguments are
appended after the configured command. Only `ssh_command[0]` expands `~`
locally. OpenOCD's first argument and remote mapping destinations expand `~`
using the SSH user's actual home, queried once only when needed during a real
operation. Recording mode leaves these remote paths unresolved and performs no
SSH.

Path mappings are YAML mappings from local to remote paths. Both sides must be
absolute or use `~` (only the current user form is supported), and neither may
contain NUL. Local paths may contain `.` and `..`; they are expanded and
resolved on the local machine before duplicate detection. Duplicate normalized
local keys are errors even when their remote destinations match. Remote paths
must already be normalized: repeated or trailing separators and `.` or `..`
components are rejected. They cannot be resolved safely while loading the
configuration because remote components may be symbolic links and loading does
not contact the remote host. Mappings refer to resources already present on the
remote host and do not stage local contents. Local and remote paths may contain
spaces; quote them as required by YAML syntax.

The schema enforces the lexical forms above. Post-schema processing is limited
to contextual semantics that cannot be expressed truthfully as string syntax:
local home expansion and filesystem resolution, normalized-local-path collision
detection, selected remote and preset references, mandatory settings for the
requested operation, and remote home expansion during a real operation.

`forward_env` contains names whose current local values may be sent to remote
OpenOCD. Values are never stored in configuration. Missing local variables are
warned about and omitted when the remote is used.
