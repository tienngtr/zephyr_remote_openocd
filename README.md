# Zephyr west runner for remote OpenOCD

This Zephyr module provides a west runner that runs OpenOCD on a remote Linux
host while keeping west, GDB, and client tools on the local machine. It accesses
the remote host through SSH.

The custom runner is intended for native Linux and WSL 2. WSL 2 validation is still
pending, and WSL 1 is not tested.

## Prerequisites and support

The supported integration boundary is Zephyr 4.4 with Python 3.12 or newer.
Native Linux is validated; WSL 2 is a supported design target whose validation
is pending; WSL 1 is unsupported.

The local machine needs a Zephyr 4.4 workspace, its configured Python
environment, west, and a board build that supports Zephyr's built-in `openocd`
runner. The remote machine needs Linux, SSH access, `python3`, and an
OpenOCD executable compatible with the board. OpenOCD and its board-support
files run remotely; west, GDB, and client tools remain local.

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
edits shell/repository files. Setup also diagnoses whether the active Python
environment provides `pyelftools`, PyYAML, and jsonschema. Zephyr 4.4's
configured Python environment provides these dependencies; if setup reports a
warning, activate or use that environment. There is no separate product package
installation step.

After activating the module, configure a remote before running a remote
operation. Add this minimum definition to the setup-created YAML file:

```yaml
default_remote: lab
remotes:
  lab:
    ssh_host: openocd_host
    openocd_command: [/absolute/path/to/openocd]
```

For an OpenOCD-capable board, Zephyr 4.4's `stm32f746g_disco` and
`samples/hello_world` provide a concrete example. From a Zephyr workspace, use:

```sh
west build -p always -b stm32f746g_disco samples/hello_world
west flash --context
west flash -r remote_openocd --remote lab
```

This board is an example, not a requirement; the runner remains board-agnostic
and works with builds that expose Zephyr's built-in `openocd` runner. Subsequent
builds can use the normal incremental `west build` command.
The context output for an OpenOCD-capable build lists both `openocd` and
`remote_openocd`.

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
    ssh_host: openocd_host
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

For detailed user guides, see [`docs/user/`](docs/user/).

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

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development rules, ordinary
checks, and links to external-test guidance.
