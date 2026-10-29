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

### 1. Obtain the module

Place this module at any persistent, user-accessible path. This example clones
it into the existing `~/zephyrproject` workspace:

```sh
cd ~/zephyrproject
git clone https://github.com/tienngtr/zephyr_remote_openocd.git
```

To use a release, check out its `v<version>` tag, such as `v0.1.0`, or extract
its source archive from the
[releases page](https://github.com/tienngtr/zephyr_remote_openocd/releases)
instead of cloning; Git is not required at run time.

> [!IMPORTANT]
> The remaining steps run from `~/zephyrproject` and assume the module is at
> `~/zephyrproject/zephyr_remote_openocd`. For another location, replace
> `zephyr_remote_openocd` and `$HOME/zephyrproject/zephyr_remote_openocd` in
> every command with the module path.

### 2. Run the setup script

Run the setup script with the Python environment configured for Zephyr 4.4.x;
the module has no separate dependency installation step:

```sh
python3 zephyr_remote_openocd/scripts/user/setup.py
```

The setup script creates `~/.config/zephyr_remote_openocd/config.yaml` without
overwriting an existing file. It prints the activation command for step 3 and
the validator command for step 5. For a configuration file at another path,
see [Location](docs/user/configuration.md#location).

**Verify:** the output starts with `Configuration (created)` or
`Configuration (already exists)`, followed by the configuration path. For an
`Error:` or a dependency `Warning:`, see
[Troubleshooting](docs/user/troubleshooting.md#the-setup-script-fails-or-warns).

### 3. Activate the module

Add the module path to the `EXTRA_ZEPHYR_MODULES` environment variable:

```sh
export EXTRA_ZEPHYR_MODULES="${EXTRA_ZEPHYR_MODULES:+$EXTRA_ZEPHYR_MODULES;}$HOME/zephyrproject/zephyr_remote_openocd"
```

To set it in every shell, put the `export` command in `~/.zephyrrc` and source
Zephyr's `zephyr-env.sh`; see
[Using `zephyrrc` files](https://docs.zephyrproject.org/4.4.0/develop/env_vars.html#option-3-using-zephyrrc-files).

**Verify:** `echo "$EXTRA_ZEPHYR_MODULES"` includes the module path in the
shell that will run `west`.

### 4. Configure the remote

Open the configuration file that the setup script created. In the `lab`
remote, replace `replace-with-ssh-host-or-alias` with the remote host name or
an alias from the normal SSH configuration. Replace the `openocd_command` path
with the path of the OpenOCD executable on the remote host. Without its
comments, the generated file contains:

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

`lab` is the remote's name, not a fixed key, and may be renamed; commands then
select it by the new name with `--remote` and `default_remote`.

For a host reached with command-line options, put the destination in
`ssh_host` and the options in `ssh_command`, one list item per argument. For
example, `ssh -p 2222 -i /home/user/.ssh/remote_key user@host.example.com`
becomes:

```yaml
    ssh_host: user@host.example.com
    ssh_command: [ssh, -p, "2222", -i, /home/user/.ssh/remote_key]
```

Alternatively, define an alias in `~/.ssh/config`, so that manual
`ssh <alias>` commands, including the checks in step 5, use the same settings
as the runner; see
[Translate a manual SSH command](docs/user/configuration.md#translate-a-manual-ssh-command).

### 5. Validate the configuration and remote prerequisites

Validate syntax and effective settings without contacting the remote. For a
configuration file at another path, use the validator command that the setup
script printed:

```sh
python3 zephyr_remote_openocd/scripts/user/validate_configuration.py --remote lab
```

Then check the remote prerequisites. Replace `replace-with-ssh-host-or-alias`
and the OpenOCD path with the values from step 4. If `ssh_command` is not plain
`[ssh]`, replace `ssh` with all of its items:

```sh
ssh replace-with-ssh-host-or-alias python3 --version
ssh replace-with-ssh-host-or-alias \
  /opt/zephyr-sdk-1.0.1/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd \
  --version
```

**Verify:** the validator prints `Configuration valid:` and the SSH host and
OpenOCD command from step 4; both `ssh` commands succeed; and the first
reports Python 3.12 or newer.

### 6. Configure the build

**A build directory registers `remote_openocd` when CMake configures it with the
module active**. This example builds `zephyr/samples/hello_world` for
`stm32f746g_disco`, a board that supports the `openocd` runner; replace them
with the board and application to use:

```sh
west build -b stm32f746g_disco zephyr/samples/hello_world
```

> [!IMPORTANT]
> A build directory configured before step 3 needs
> `west build --cmake-only` once; an ordinary `west build`, `west flash`, or
> `west debug` is not guaranteed to reconfigure it. Add `-d <build>` only for a
> directory other than the default. A pristine build is not required.

**Verify:** `west flash --context` lists both runners in this section:

```text
available runners in runners.yaml:
  ..., openocd, ..., remote_openocd
```

The earlier `zephyr runners which support "west flash":` section does not
reflect this build directory, so it can list `remote_openocd` even when the
build cannot use it. If either runner is missing, see
[Troubleshooting](docs/user/troubleshooting.md#remote_openocd-is-not-listed-by-west-flash---context).

### 7. Flash the board

> [!WARNING]
> If the remote host has more than one probe attached, add `--serial <serial>`
> with the serial number of the target board's probe; otherwise OpenOCD may
> program a different board. See
> [Runner, remote, and probe options](docs/user/operations.md#runner-remote-and-probe-options).

```sh
west flash -r remote_openocd --remote lab
```

**Verify:** the command succeeds, and OpenOCD output appears in the local
terminal. For failures, see [Troubleshooting](docs/user/troubleshooting.md).

### 8. Optional: make `remote_openocd` the default

To run plain `west flash` and `west debug` through the remote, change
`default_runner` to `remote_openocd` and uncomment `default_remote: lab` near
the top of the configuration file:

```yaml
default_runner: remote_openocd
default_remote: lab
```

The next `west flash`, `west debug`, or `west build` applies the new default
runner to the build directory; a pristine build is not required:

```sh
west flash
```

**Verify:** the command flashes through remote OpenOCD as in step 7.

The defaults do not select a probe. On a host with several probes, keep
passing `--serial <serial>`, for example `west flash --serial <serial>`.
`west flash -r openocd` still uses local OpenOCD. For build-time runner
overrides, see
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
