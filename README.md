# Zephyr Remote OpenOCD

This self-contained Zephyr module runs OpenOCD on a remote Linux host while
keeping west and GDB local. It may be stored at any persistent path;
`~/zephyrproject/zephyr-remote-openocd` is a convenient example.

Run setup from the module root:

```sh
python3 scripts/setup.py
```

Setup creates `~/.config/zephyr-remote-openocd/config.toml` from the shipped
template when absent, preserves existing configuration, reports dependency
status, and prints the module path to place in `EXTRA_ZEPHYR_MODULES`. It does
not edit shell files or repositories.

The canonical commented V1 schema is
[`resources/config.toml.example`](resources/config.toml.example). Its only keys
are:

- `zephyr.default`: `"local"` or `"remote"`; defaults to `"local"`.
- `remote.host`: non-empty SSH host or alias, required for remote operations.
- `remote.openocd`: normalized absolute remote POSIX executable path, required
  for remote operations.
- `ssh.command`: non-empty string argv; defaults to `["ssh"]`.
- `openocd.forward_env`: unique environment-variable names; defaults to `[]`.
- `paths.map`: recursive `{local, remote}` mappings. Local paths are absolute
  after optional `~` expansion; remote paths are normalized absolute POSIX
  paths. The longest matching local prefix wins.

Unknown keys, malformed TOML, invalid types, empty values, and duplicate or
conflicting mappings are rejected with the configuration path in the error.
