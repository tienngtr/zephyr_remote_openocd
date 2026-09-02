# Configuration

Run `python3 /path/to/zephyr_remote_openocd/scripts/setup.py`. Setup creates
`~/.config/zephyr_remote_openocd/config.toml` once and never overwrites or
changes an existing file. Set `ZEPHYR_REMOTE_OPENOCD_CONFIG` to select another
configuration path, for example:

```sh
export ZEPHYR_REMOTE_OPENOCD_CONFIG="$HOME/.config/zephyr_remote_openocd/lab.toml"
```

The shipped [`config.toml.example`](../../resources/config.toml.example) is the
canonical commented template. V1 has a deliberately small, strict schema:

- `runner.default` is `openocd` or `remote_openocd`.
- `[remote]` contains `host` and an absolute remote `openocd` path.
- `[ssh].command` is an argv array; it is not shell syntax.
- `[openocd].forward_env` is the allow-list for variables copied to the remote
  helper/OpenOCD process.
- `[[paths.map]]` recursively maps normalized absolute local prefixes to
  normalized absolute remote POSIX prefixes. The longest matching prefix wins.

Mappings refer to resources already installed on the remote host; they do not
synchronize local contents. A file or directory under a mapped local prefix is
therefore not staged, and local edits are not copied remotely. Omit the mapping
when the resource should be staged into each session.

Unknown keys, malformed TOML, empty values, invalid paths, and duplicate or
conflicting mappings are rejected with an actionable diagnostic. Only
allow-listed variables that exist locally are forwarded. Product setup does
not install packages; use the Python environment configured for Zephyr, which
normally provides `pyelftools` for ELF inspection.
