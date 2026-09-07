# Zephyr west runner for remote OpenOCD

This Zephyr module provides a west runner that runs OpenOCD on a remote Linux
host while keeping west, GDB, and client tools on the local machine. It accesses
the remote host through SSH.

The custom runner is intended for native Linux and WSL 2. WSL 2 validation is still
pending, and WSL 1 is not tested.

## Install and activate

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

The setup script creates `~/.config/zephyr_remote_openocd/config.yaml` if it
does not exist (or reports its existing path), prints the absolute paths of the
configuration file and module root, and gives equivalent
`EXTRA_ZEPHYR_MODULES` guidance. It never overwrites an existing config or
edits shell/repository files. Setup also checks whether `pyelftools` is
available because some commands may need to inspect ELF files. If setup reports
a warning, use the Python environment configured for Zephyr before running
commands that inspect ELF files.

The YAML configuration loader requires PyYAML and jsonschema. Install
`requirements.txt` into the Python environment used by Zephyr when those
packages are not already available; setup reports their status but does not
install packages.

After activating the module, configure an application for an OpenOCD-capable
board so Zephyr can discover the module and add `remote_openocd` alongside the
built-in `openocd` runner. The easiest way to do this is with a pristine build:

```sh
west build -p always -b <board> <application>
```

Subsequent builds can use the normal incremental `west build` command. To
inspect the generated runner list, run `west flash --context`; an
OpenOCD-capable build lists both `openocd` and `remote_openocd`.

## Runner selection

The default configuration uses the built-in local `openocd` runner:

```yaml
default_runner: openocd
```

`west` commands without an explicit runner selection use the built-in `openocd`
runner. To select the remote runner, specify it explicitly, for example:

```sh
west flash -r remote_openocd
```

To make the remote runner the default, change `default_runner` to
`remote_openocd` in the setup-created configuration file. Then run
`west build` again; an incremental build is enough for CMake to regenerate the
runner configuration.

## Remote configuration

The default local `openocd` runner does not need remote settings. To use
`remote_openocd`, define a named remote in the YAML configuration:

```yaml
default_remote: lab
remotes:
  lab:
    ssh_host: openocd-host
    openocd_command:
      - /absolute/path/to/openocd
```

Use `west ... -r remote_openocd --remote lab` to select a remote. SSH command,
environment forwarding, and path mappings are configured under the remote or a
reusable `presets` entry.

The shipped [`config.yaml.example`](resources/config.yaml.example) shows the
available fields. The [`configuration guide`](docs/user/configuration.md)
explains the full schema and `ZEPHYR_REMOTE_OPENOCD_CONFIG` if you need a
different config path.

## Operations

For supported workflows, use the same `west` operations as with the built-in
`openocd` runner, selecting this runner with `-r remote_openocd`:

```sh
west flash -r remote_openocd
west debug -r remote_openocd
west attach -r remote_openocd
west debugserver -r remote_openocd
west rtt -r remote_openocd
west debug -r remote_openocd --rtt-server
west debugserver -r remote_openocd --rtt-server
```

`remote_openocd` acts like Zephyr's built-in `openocd` runner for the supported
west workflows, but runs OpenOCD on the configured remote host and forwards its
enabled services to the local machine over SSH.

The direct semihosting console uses ordinary user-supplied OpenOCD commands
(typically passed through `--cmd-pre-init`) and relays normal OpenOCD
stdout/stderr. It intentionally adds no semihosting proxy, filesystem
virtualization, TCP redirect, or GDB File-I/O path. See the
[`operations guide`](docs/user/operations.md) for usage details.

Remote sessions relay OpenOCD output to the local terminal and clean up
automatically when the operation ends, fails, is interrupted, or loses its SSH
connection. See the [`troubleshooting guide`](docs/user/troubleshooting.md) for
lifecycle details.

For detailed user guides, see [`docs/user/`](docs/user/). For contributor
testing and external fixture prerequisites, see
[`docs/development/testing.md`](docs/development/testing.md) and
[`docs/development/hardware-fixtures.md`](docs/development/hardware-fixtures.md).

## Uninstall

Stop any active west operation before removing the module. Delete the module
copy and, if no longer needed, the configuration file created by setup:

```sh
rm -rf "$HOME/zephyrproject/zephyr_remote_openocd"
rm -rf "$HOME/.config/zephyr_remote_openocd"
```

Remove the activation export from the current shell and from `~/.zephyrrc` if
you added it. On each configured remote host, after all sessions have stopped,
remove the runner's remote files and any empty session root:

```text
~/.local/libexec/zephyr_remote_openocd/
$XDG_RUNTIME_DIR/zephyr_remote_openocd/
~/.cache/zephyr_remote_openocd/sessions/
```
