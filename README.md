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
[`resources/config.toml.example`](resources/config.toml.example). The frozen
schema, validation rules, environment allow-list, and path-mapping/staging
semantics are described in the
[`configuration guide`](docs/user/configuration.md).

The default `command = ["ssh"]` works on native Linux and WSL2 when Linux
OpenSSH is available. Add fixed arguments when needed:

```toml
[ssh]
command = ["ssh", "-F", "/home/user/.ssh/lab_config", "-o", "ControlMaster=no"]
```

WSL2 users may optionally select Windows OpenSSH instead, for example when
they want to reuse Windows-side SSH configuration or credentials:

```toml
[ssh]
command = ["/mnt/c/Windows/System32/OpenSSH/ssh.exe", "-o", "ControlMaster=no"]
```

WSL-specific validation is not yet part of the validated native-Linux support
claim; see [`docs/validation/v1.md`](docs/validation/v1.md) for platform status.

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
are in [`docs/requirements/SRS.md`](docs/requirements/SRS.md) and
[`docs/architecture/SAD.md`](docs/architecture/SAD.md).

Protocol 1 is the frozen JSON-lines helper contract. The helper is deployed
automatically to the remote user account and cleans up sessions on normal or
abnormal termination. Compatibility rules and the complete wire contract are
documented in [`docs/architecture/protocol-v1.md`](docs/architecture/protocol-v1.md).

For detailed user guides, see [`docs/user/`](docs/user/). For contributor
testing and external fixture prerequisites, see
[`docs/development/testing.md`](docs/development/testing.md) and
[`docs/development/hardware-fixtures.md`](docs/development/hardware-fixtures.md).
