# Zephyr west runner for remote OpenOCD

This Zephyr module provides a west runner that runs OpenOCD on a remote Linux
host while keeping west, GDB, and client tools on the local machine. It accesses
the remote host through SSH.

The project is under active development and has not produced a release. Its
current integration target is Zephyr 4.4.

## Prerequisites and support

The supported integration boundary is Zephyr 4.4 with Python 3.12 or newer.
Native Linux is validated; WSL 2 is a supported design target whose validation
is pending; WSL 1 is unsupported.

The local machine needs a Zephyr 4.4 workspace, its configured Python
environment, west, and a board build that supports Zephyr's built-in `openocd`
runner. The remote machine needs Linux, SSH access, `python3`, and an
OpenOCD executable compatible with the board. OpenOCD and its board-support
files run remotely; west, GDB, and client tools remain local.

## Quick start

Place a checkout, a filesystem copy, or an extracted archive of this module at
any persistent, user-accessible path. For example, you can clone the module into
the existing `~/zephyrproject` workspace:

```sh
cd ~/zephyrproject
git clone https://github.com/tienngtr/zephyr_remote_openocd.git
```

Then run the setup script:

```sh
python3 zephyr_remote_openocd/scripts/setup.py
```

Activate the module by adding its path to the `EXTRA_ZEPHYR_MODULES` environment
variable:

```sh
export EXTRA_ZEPHYR_MODULES="${EXTRA_ZEPHYR_MODULES:+$EXTRA_ZEPHYR_MODULES;}$HOME/zephyrproject/zephyr_remote_openocd"
```

To avoid entering the command manually, put the `export` command above in
`~/.zephyrrc`, then source Zephyr's `zephyr-env.sh` script to activate it. See
[Using `zephyrrc` files](https://docs.zephyrproject.org/4.4.0/develop/env_vars.html#option-3-using-zephyrrc-files)
in the Zephyr documentation for more details.

Setup creates `~/.config/zephyr_remote_openocd/config.yaml` without overwriting
an existing file. It also reports the module path and diagnoses required Python
dependencies. If it reports a dependency warning, use Zephyr 4.4's configured
Python environment; there is no separate product installation step.

Configure a remote before running a remote operation. The example below
declares `lab` as the default remote. SSH uses the same host name, and OpenOCD
comes from Zephyr SDK 1.0.1 installed under `/opt` on that machine:

```yaml
default_runner: openocd

default_remote: lab

presets:
  default:
    openocd_command:
      - /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd
    forward_env: []
    # Keys are local paths and values are normalized remote POSIX paths.
    path_mappings: {}

remotes:
  lab:
    preset: default
```

Verify the remote prerequisites using the same host name and OpenOCD path:

```sh
ssh lab python3 --version
ssh lab /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd --version
```

Both commands must succeed. If your SSH command needs fixed options, configure
`ssh_command` and run the equivalent check with those options.

The runner is board-agnostic and works with builds that expose Zephyr's built-in
`openocd` runner. `stm32f746g_disco` is a board that supports the `openocd`
runner and will be used together with `samples/hello_world` to provide a
concrete example. From a Zephyr workspace, run:

```sh
west build -p always -b stm32f746g_disco samples/hello_world
west flash --context
```

A pristine build is needed to add `remote_openocd` to available runners of the
build. You should see that both `openocd` and `remote_openocd` are listed.
Subsequent builds can use the normal incremental `west build` command. Now run:

```sh
west flash -r remote_openocd --remote lab
```

to flash the board through the remote OpenOCD. The `--remote lab` part can be
omitted because `lab` is the default remote.

The built-in local runner remains the default. To make `remote_openocd` the
default, update `default_runner` and run an incremental `west build` so CMake
regenerates the runner configuration.

## How it works

1. West and the Zephyr build remain on the local machine.
2. The runner selects a configured SSH host and remote OpenOCD executable.
3. Required build and board-support files are mapped or copied to a temporary
   remote session directory.
4. OpenOCD runs remotely while its enabled services are forwarded to local
   loopback ports for GDB and RTT clients.
5. OpenOCD output is relayed locally, and session processes and temporary files
   are cleaned up when the command ends or the SSH connection is lost.

## Next steps

- [`Configuration`](docs/user/configuration.md): defaults, presets, SSH
  arguments, environment forwarding, path mappings, and validation rules.
- [`Operations`](docs/user/operations.md): flash, debug, attach, debugserver,
  RTT, semihosting, cleanup, and uninstall.
- [`Troubleshooting`](docs/user/troubleshooting.md): common setup, SSH,
  staging, endpoint, and lifecycle failures.
- [`Contributing`](CONTRIBUTING.md): developer setup, repository structure,
  tests, and contribution rules.
