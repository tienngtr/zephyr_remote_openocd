# Configuration

Run `python3 scripts/setup.py` from any persistent copy of the module. Setup
creates `~/.config/zephyr-remote-openocd/config.toml` once and never overwrites
or changes an existing file. Set `ZEPHYR_REMOTE_OPENOCD_CONFIG` to use another
configuration file; its path may be anywhere accessible to the user.

The shipped [`config.toml.example`](../../resources/config.toml.example) is the
canonical commented template. V1 has a deliberately small, strict schema:

- `[zephyr].default` is `local` or `remote`.
- `[remote]` contains `host` and an absolute remote `openocd` path.
- `[ssh].command` is an argv array; it is not shell syntax.
- `[openocd].forward_env` is the allow-list for variables copied to the remote
  helper/OpenOCD process.
- `[[paths.map]]` recursively maps normalized absolute local prefixes to
  normalized absolute remote POSIX prefixes. The longest matching prefix wins.

Unknown keys, malformed TOML, empty values, invalid paths, and duplicate or
conflicting mappings are rejected with an actionable diagnostic. Only
allow-listed variables that exist locally are forwarded. Product setup does
not install packages; use the Python environment configured for Zephyr, which
normally provides `pyelftools` for ELF inspection.
