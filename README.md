# Zephyr Remote OpenOCD

Zephyr Remote OpenOCD is a self-contained Zephyr module. It keeps west, GDB,
and client tools on the development machine while running OpenOCD on a
configured remote Linux host. No pip installation is required.

## Install and activate

Obtain a checkout or filesystem/archive copy and put it at any persistent,
user-accessible location. This guide uses
`~/zephyrproject/zephyr-remote-openocd` as an example; the implementation does
not depend on that path.

```sh
cd ~/zephyrproject/zephyr-remote-openocd
python3 scripts/setup.py
export EXTRA_ZEPHYR_MODULES="$PWD"
```

Setup creates (or reports the existing)
`~/.config/zephyr-remote-openocd/config.toml`, prints its absolute path and the
module root, and gives equivalent `EXTRA_ZEPHYR_MODULES` guidance. It never
overwrites an existing config or edits shell/repository files. The default
config directory/file created by setup are mode `0700`/`0600`; existing paths
are not chmodded. The `pyelftools` check is non-fatal: use the Python
environment configured for Zephyr, which is expected to provide `elftools` for
ELF inspection.

Configure and build applications normally:

```sh
west build -b <board> <application> -d build
```

## Runner selection

The shipped template is the frozen V1 schema. The default is local:

```toml
[zephyr]
default = "local" # or "remote"
```

With that default, ordinary `west flash`/`west debug` selects the built-in
`openocd` runner. Selection can always be explicit:

```sh
west flash -d build -r openocd
west flash -d build -r remote-openocd
```

Change `[zephyr].default` to `"remote"` for normal west invocations to select
`remote-openocd` (and back to `"local"` for the built-in runner); rerun the
normal west command so its build configuration can regenerate as needed.

## Remote configuration

Edit the setup-created `~/.config/zephyr-remote-openocd/config.toml`. The
runner may instead read a different file when
`ZEPHYR_REMOTE_OPENOCD_CONFIG` is set. The canonical commented template is
[`resources/config.toml.example`](resources/config.toml.example):

```toml
[remote]
host = "openocd-host"
openocd = "/absolute/path/to/openocd"

[ssh]
command = ["ssh"]

[openocd]
forward_env = ["FTDI_CHANNEL"]

[[paths.map]]
local = "/home/user/openocd/scripts"
remote = "/opt/openocd/scripts"
```

V1 fields are limited to the local/remote default, `remote.host`, an absolute
remote OpenOCD path, an SSH argv command, an environment-variable allow-list,
and recursive local-to-remote path mappings. Unknown keys, malformed TOML,
empty/invalid values, and duplicate or conflicting mappings are errors. The
SSH command is argv (not shell text); only allow-listed variables that exist
locally are forwarded. Mappings use normalized absolute paths and the longest
matching local prefix.

Override the SSH executable or add fixed arguments when needed, including in
WSL2:

```toml
[ssh]
command = ["/mnt/c/Windows/System32/OpenSSH/ssh.exe", "-o", "ControlMaster=no"]
```

WSL-specific validation remains deferred (PG-012/PG-013).

## Operations

Use the same west operations with `-r remote-openocd`:

```sh
west flash -d build -r remote-openocd
west debug -d build -r remote-openocd
west attach -d build -r remote-openocd
west debugserver -d build -r remote-openocd
west rtt -d build -r remote-openocd
west debug -d build -r remote-openocd --rtt-server
west debugserver -d build -r remote-openocd --rtt-server
```

`debug` follows Zephyr's load behavior; `attach` does not flash/load solely
because the target is remote; `debugserver` exposes the GDB endpoint without
launching local GDB. Standalone `rtt` launches the local channel-0 client;
`--rtt-server` exposes RTT alongside the session but does not launch a local
RTT client automatically. All enabled GDB, Tcl, and telnet services are
forwarded, while disabled services have no listener or readiness requirement.

Direct semihosting console use ordinary fixture-supplied OpenOCD commands
(typically through `--cmd-pre-init`) and the existing OpenOCD stdout/stderr
relay. It intentionally adds no semihosting proxy, filesystem virtualization,
TCP redirect, or GDB File-I/O path. Detailed lifecycle and protocol behavior
are in [`doc/SRS.md`](doc/SRS.md) and [`doc/SAD.md`](doc/SAD.md).

Protocol 1 is the frozen JSON-lines helper contract. The helper is deployed
automatically to the remote user account and cleans up sessions on normal or
abnormal termination. Compatibility rules and the complete wire contract are
documented in the SAD.

For test-fixture setup and hardware prerequisites, see
[`tests/README.md`](tests/README.md).
