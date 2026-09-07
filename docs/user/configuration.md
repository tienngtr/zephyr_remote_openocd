# Configuration

Run `python3 /path/to/zephyr_remote_openocd/scripts/setup.py` to create the
canonical file `~/.config/zephyr_remote_openocd/config.yaml`. Setup never
overwrites an existing file. Set `ZEPHYR_REMOTE_OPENOCD_CONFIG` to read a
different file; its contents are YAML regardless of its filename extension.

The complete machine-readable contract is
[`configuration.schema.json`](../requirements/configuration.schema.json), and
the commented template is [`config.yaml.example`](../../resources/config.yaml.example).
An empty YAML document is equivalent to `{}`. Unknown keys, duplicate YAML
mapping keys, null values, and type mismatches are errors.

## Top-level settings

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
    ssh_host: lab-alias
```

`default_runner` defaults to `openocd` and may be `remote_openocd`. Presets and
remotes are optional mappings. Names are case-sensitive and must match
`[A-Za-z0-9][A-Za-z0-9._-]*`.

A remote may be standalone or may name one preset. Remote fields replace the
corresponding preset setting as a whole; lists and mappings are never merged.
Use `forward_env: []` or `path_mappings: {}` to erase an inherited collection.
Missing optional values receive `ssh_command: [ssh]`, an empty environment
list, and empty path mappings. `ssh_host` defaults to the remote name.

Remote selection uses `--remote NAME`, then the non-empty
`ZEPHYR_REMOTE_OPENOCD_REMOTE` environment variable, then `default_remote`.
An empty environment variable is treated as unset. A selected remote and its
named preset must exist, and a production operation must provide
`openocd_command`; unused incomplete definitions are allowed.

## Commands and paths

Commands are argv sequences, not shell strings. The first element must be a
bare executable name, an absolute path, or `~/path`; later elements are literal
strings and may be empty. Runner-generated OpenOCD arguments are appended after
the configured command. Only `ssh_command[0]` expands `~` locally. OpenOCD's
first argument and remote mapping destinations expand `~` using the SSH user's
actual home, queried once only when needed during a real operation. Recording
mode leaves these remote paths unresolved and performs no SSH.

Path mappings are YAML mappings from local to remote paths. Both sides must be
absolute or use `~` (only the current user form is supported). Local paths are
expanded and normalized before duplicate detection; duplicate normalized local
keys are errors even when their remote destinations match. Mappings refer to
resources already present on the remote host and do not stage local contents.

`forward_env` contains names whose current local values may be sent to remote
OpenOCD. Values are never stored in configuration. Missing local variables are
warned about and omitted when the remote is used.
