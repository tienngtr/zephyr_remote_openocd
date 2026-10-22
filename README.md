# Zephyr west runner for remote OpenOCD

This Zephyr module provides a west runner that runs OpenOCD on a remote Linux
host while keeping west, GDB, and client tools on the local machine. It accesses
the remote host through SSH.

The product version is recorded in [`VERSION`](VERSION), and releases are
tagged `v<version>`, for example `v0.1.0`. Its integration target is the Zephyr
4.4 release series (4.4.x).

## Prerequisites and support

The supported integration boundary is the Zephyr 4.4 release series (4.4.x) on
Linux with Python 3.12 or newer.

The local machine needs a Zephyr 4.4.x workspace, its configured Python
environment, west, and a board build that supports Zephyr's built-in `openocd`
runner. The remote machine needs Linux, SSH access, `python3` version 3.12 or
newer, and an OpenOCD executable compatible with the board. OpenOCD and its
board-support files run remotely; west, GDB, and client tools remain local.

## Quick start

> [!IMPORTANT]
> Complete these steps in order. The `remote_openocd` runner is available only
> in a build directory that was configured **after** the module was activated in
> [step 3](#3-activate-the-module).

### 1. Obtain the module

Place a checkout, a filesystem copy, or an extracted archive of this module at
any persistent, user-accessible path. For example, you can clone the module into
the existing `~/zephyrproject` workspace:

```sh
cd ~/zephyrproject
git clone https://github.com/tienngtr/zephyr_remote_openocd.git
```

To use a release, check out its tag in that clone, or extract its source
archive from the repository's GitHub releases page in place of cloning. Git is
not required at run time.

### 2. Run the setup script

```sh
python3 zephyr_remote_openocd/scripts/user/setup.py
```

Setup creates `~/.config/zephyr_remote_openocd/config.yaml` without overwriting
an existing file. It also reports the module path and diagnoses required Python
dependencies. If it reports a dependency warning, use the supported Zephyr
4.4.x environment's configured Python environment; there is no separate
product installation step.

### 3. Activate the module

Add the module path to the `EXTRA_ZEPHYR_MODULES` environment variable:

```sh
export EXTRA_ZEPHYR_MODULES="${EXTRA_ZEPHYR_MODULES:+$EXTRA_ZEPHYR_MODULES;}$HOME/zephyrproject/zephyr_remote_openocd"
```

To avoid entering the command manually, put the `export` command above in
`~/.zephyrrc`, then source Zephyr's `zephyr-env.sh` script to activate it. See
[Using `zephyrrc` files](https://docs.zephyrproject.org/4.4.0/develop/env_vars.html#option-3-using-zephyrrc-files)
in the Zephyr documentation for more details.

**Verify:** `echo "$EXTRA_ZEPHYR_MODULES"` includes the module path in the
shell that will run `west`.

### 4. Configure the remote

The generated configuration contains a safe, unselected `lab` remote. Replace
its SSH host and remote OpenOCD path:

```yaml
default_runner: openocd

remotes:
  lab:
    ssh_host: replace-with-ssh-host-or-alias
    openocd_command:
      - /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd
    ssh_command:
      - ssh
    forward_env: []
    path_mappings: {}
```

### 5. Validate the configuration and remote prerequisites

Validate syntax and effective settings without contacting the remote:

```sh
python3 zephyr_remote_openocd/scripts/user/validate_configuration.py --remote lab
```

Then verify the remote prerequisites using the configured host and OpenOCD
path:

```sh
ssh replace-with-ssh-host-or-alias python3 --version
ssh replace-with-ssh-host-or-alias \
  /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd \
  --version
```

**Verify:** both commands succeed, and the first reports Python 3.12 or newer.

If your SSH command needs fixed options, configure `ssh_command` and run the
equivalent check with those options. Custom wrappers must also support the
[stdin half-close required for session cleanup](docs/user/configuration.md#custom-ssh-arguments).

### 6. Configure the build

The runner is board-agnostic and works with builds that expose Zephyr's built-in
`openocd` runner. This example uses `samples/hello_world` on
`stm32f746g_disco`, a board that supports the `openocd` runner. From a Zephyr
workspace, build it:

```sh
west build -b stm32f746g_disco samples/hello_world
```

Then reconfigure the build directory so that it registers `remote_openocd`:

```sh
west build --cmake-only
```

> [!WARNING]
> A build directory configured before step 3 does not register
> `remote_openocd`, and an ordinary incremental `west build` does not add it.
> Always run `west build --cmake-only` for an existing build directory. It is
> harmless for a fresh build, and a pristine build is not required.

**Verify:** `west flash --context` lists both `openocd` and `remote_openocd`.
If `remote_openocd` is missing, confirm step 3 in the current shell and repeat
this step.

### 7. Flash the board

```sh
west flash -r remote_openocd --remote lab
```

Uncomment `default_remote: lab` in the configuration if you want to omit
`--remote lab` later. To make `remote_openocd` the default flash and debug
runner, see the `default_runner` description in
[Configuration](docs/user/configuration.md#quick-start).

## How it works

1. West and the Zephyr build remain on the local machine.
2. The runner uses the configured SSH command to deploy a remote helper and
   create an isolated session.
3. The local runner communicates with the helper over SSH to stage the build
   and board-support files and start OpenOCD; the helper supervises the remote
   process and emits structured lifecycle and output events.
4. SSH forwarding exposes required OpenOCD services on local loopback ports
   and independently attempts best-effort forwarding for Tcl and telnet.
   Explicitly requested RTT forwarding is required. Best-effort availability
   failures produce warnings; cleanup failures remain fatal.
5. The runner relays output and cleans up the remote session when it exits, is
   interrupted, or detects that SSH connectivity was lost. The remote helper
   starts its own cleanup after it detects control-channel loss.

SSH connection sharing remains managed by your client. The runner cleans up
its launched SSH subprocesses and owned I/O resources; an external sharing
master may retain forwards. A best-effort local preferred address cache helps
reuse matching endpoints without bypassing helper leases or remote-port checks.
Stale retained forwards may still need
[user cleanup](docs/user/troubleshooting.md#ssh-connection-sharing).

## Next steps

- [`Configuration`](docs/user/configuration.md): defaults, presets, SSH
  arguments, environment forwarding, path mappings, and validation rules.
- [`Operations`](docs/user/operations.md): flash, debug, attach, debugserver,
  RTT, semihosting, cleanup, and uninstall.
- [`Troubleshooting`](docs/user/troubleshooting.md): common setup, SSH,
  staging, endpoint, and lifecycle failures.
- [`Contributing`](CONTRIBUTING.md): developer setup, repository structure,
  tests, and contribution rules.
