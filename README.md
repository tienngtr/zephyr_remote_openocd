# Zephyr west runner for remote OpenOCD

This Zephyr module provides a west runner, `remote_openocd`, for boards
attached to a remote Linux host. OpenOCD runs on the remote host, which the
runner accesses through SSH; west, GDB, and client tools stay on the local
machine. The runner supports the Zephyr 4.4 release series (4.4.x) on Linux
with Python 3.12 or newer.

## Prerequisites and support

Before using the runner, make sure that:

- **The local machine** runs Linux with Python 3.12 or newer, a Zephyr 4.4.x
  workspace with its configured Python environment, and west.
- **The board build** supports Zephyr's built-in `openocd` runner.
- **The local SSH client** meets the
  [SSH client requirements](docs/user/configuration.md#ssh-client-requirements),
  including the
  [stdin half-close](docs/user/configuration.md#stdin-half-close) that session
  cleanup depends on. The default SSH command is `ssh`.
- **The remote host** runs Linux, accepts SSH access, and provides `python3`
  version 3.12 or newer and an OpenOCD executable compatible with the board.
- **The board's debug probe** is connected to the remote host, and the remote
  SSH account has permission to use it.

Routine runner use and cleanup do not require root privileges on either
machine.

## Quick start

> [!IMPORTANT]
> Complete these steps in order. The `remote_openocd` runner is available only
> in a build directory that was configured **after** the module was activated in
> [step 3](#3-activate-the-module).

### 1. Obtain the module

Place a checkout, a filesystem copy, or an extracted archive of this module at
any persistent, user-accessible path. For example, clone the module into the
existing `~/zephyrproject` workspace:

```sh
cd ~/zephyrproject
git clone https://github.com/tienngtr/zephyr_remote_openocd.git
```

The module version is recorded in [`VERSION`](VERSION), and releases are
tagged `v<version>`, for example `v0.1.0`. To use a release, check out its tag
in that clone, or extract its source archive from the repository's
[GitHub releases page](https://github.com/tienngtr/zephyr_remote_openocd/releases)
in place of cloning. Git is not required at run time.

The remaining steps run from the Zephyr workspace, `~/zephyrproject` in this
example, and use this example module location. For another module location,
replace `zephyr_remote_openocd` and `$HOME/zephyrproject/zephyr_remote_openocd`
in the commands with the module path.

### 2. Run the setup script

Run the setup script with the Python environment configured for Zephyr 4.4.x:

```sh
python3 zephyr_remote_openocd/scripts/user/setup.py
```

The setup script creates `~/.config/zephyr_remote_openocd/config.yaml` without
overwriting an existing file. To use a configuration file at another path,
pass that path as an argument, then set `ZEPHYR_REMOTE_OPENOCD_CONFIG` with the
`export` command that the setup script prints so that the build and
`remote_openocd` commands read that file; see
[Location](docs/user/configuration.md#location).

The setup script prints:

- the configuration path;
- the module path;
- the activation command for step 3, with the module path filled in;
- a validator command for step 5, with `NAME` in place of the remote name;
- for a path other than the default, the command that points the build and
  `remote_openocd` commands at that file;
- whether the required Python dependencies are available.

**Verify:** the output starts with `Configuration (created)` or
`Configuration (already exists)`, followed by the configuration path.

A dependency warning does not stop the setup script from creating the
configuration. Use the Python environment configured for Zephyr 4.4.x; the
module has no separate dependency installation step. If the setup script
fails, see
[Troubleshooting](docs/user/troubleshooting.md#the-setup-script-fails-or-warns).

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

Open the configuration file created by the setup script. In the `lab` remote,
replace the `ssh_host` value with the remote host name or an alias from the
normal SSH configuration, and replace the `openocd_command` path with the path
of the OpenOCD executable on the remote host. Without its comments, the
generated file contains:

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

Nothing uses the generated `lab` remote until a runner command selects it,
so leaving it unedited is safe; see
[Remote selection order](docs/user/configuration.md#remote-selection-order).

### 5. Validate the configuration and remote prerequisites

Validate syntax and effective settings without contacting the remote:

```sh
python3 zephyr_remote_openocd/scripts/user/validate_configuration.py --remote lab
```

For a configuration file at another path, put that path before `--remote`,
as in the validator command that the setup script printed.

Then verify the remote prerequisites. In these commands, replace
`replace-with-ssh-host-or-alias` and the OpenOCD path with the values entered
in step 4. If `ssh_command` uses another executable or fixed options, see
[Custom SSH arguments](docs/user/configuration.md#custom-ssh-arguments), and
run these checks with the executable and fixed options from `ssh_command` in
place of `ssh`:

```sh
ssh replace-with-ssh-host-or-alias python3 --version
ssh replace-with-ssh-host-or-alias \
  /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd \
  --version
```

**Verify:** both commands succeed, and the first reports Python 3.12 or newer.

### 6. Configure the build

This example builds `zephyr/samples/hello_world` for `stm32f746g_disco`, a
board that supports the `openocd` runner. Replace `stm32f746g_disco` and
`zephyr/samples/hello_world` with the board and application to use:

```sh
west build -b stm32f746g_disco zephyr/samples/hello_world
```

Then reconfigure the build directory. This registers `remote_openocd` in a
build directory configured before step 3, which an ordinary incremental
`west build` does not do, and is harmless for a fresh build such as the one
above. Always run it for an existing build directory; a pristine build is not
required:

```sh
west build --cmake-only
```

**Verify:** `west flash --context` lists both `openocd` and `remote_openocd`.
If `remote_openocd` is missing, confirm step 3 in the current shell and repeat
this step. If `openocd` is also missing, see
[Troubleshooting](docs/user/troubleshooting.md#remote_openocd-is-not-listed-by-west-flash---context).

### 7. Flash the board

```sh
west flash -r remote_openocd --remote lab
```

The runner programs the image through remote OpenOCD and starts it.

**Verify:** the command succeeds, and OpenOCD output appears in the local
terminal. For failures, see [Troubleshooting](docs/user/troubleshooting.md).

To omit `--remote lab` later, uncomment the `# default_remote: lab` line near
the top of the configuration file. To make `remote_openocd` the default flash
and debug runner, see
[Choose the default remote and runner](docs/user/configuration.md#choose-the-default-remote-and-runner).

## How it works

For each operation, the runner uses the configured SSH command to:

1. deploy the remote helper, a Python program that runs with the remote
   `python3`;
2. copy the required build output and board-support files to the remote host,
   unless a path mapping supplies them; see
   [Files copied to the remote](docs/user/configuration.md#files-copied-to-the-remote);
3. start OpenOCD through the remote helper and relay its output to the local
   terminal;
4. forward the OpenOCD services selected by the command and its port options
   to local loopback ports; see
   [Forwarded services](docs/user/operations.md#forwarded-services);
5. ask the remote helper to clean up the session when the operation ends or is
   interrupted.

If the local SSH client reports that the connection was lost, the runner fails
the operation and attempts local cleanup, and the remote helper begins cleaning
up the session after it observes the loss; see
[Exit status and connection loss](docs/user/operations.md#exit-status-and-connection-loss).
Forwards kept by SSH connection sharing can outlive a session and may need
cleanup through the SSH client; see
[SSH connection sharing](docs/user/troubleshooting.md#ssh-connection-sharing).

## Next steps

- [Configuration](docs/user/configuration.md): defaults, presets, SSH
  arguments, environment forwarding, path mappings, and validation rules.
- [Operations](docs/user/operations.md): flash, debug, attach, debugserver,
  RTT, semihosting, cleanup, and uninstall.
- [Troubleshooting](docs/user/troubleshooting.md): common setup, SSH, file
  copy, port, and session-end failures.
- [Contributing](CONTRIBUTING.md): developer setup, repository structure,
  tests, and contribution rules.
