# Operations

Activate the module by appending its path to the semicolon-separated
`EXTRA_ZEPHYR_MODULES` list, for example:

```sh
export EXTRA_ZEPHYR_MODULES="${EXTRA_ZEPHYR_MODULES:+$EXTRA_ZEPHYR_MODULES;}/path/to/zephyr_remote_openocd"
```

For shell activation in every Zephyr session, put the same export in
`~/.zephyrrc`. Then build a
Zephyr application normally. The built-in `openocd` runner remains the default
unless `default_runner: remote_openocd` is selected.

For a concrete Zephyr 4.4.x example, build the `samples/hello_world` application
for the OpenOCD-capable `stm32f746g_disco` board, inspect the available
runners, and select the `lab` remote from your configuration:

```sh
west build -b stm32f746g_disco samples/hello_world
west flash --context
west flash -r remote_openocd --remote lab
west debug -r remote_openocd --remote lab
west attach -r remote_openocd --remote lab
west debugserver -r remote_openocd --remote lab
```

The board and application are examples, not product requirements. Substitute
any Zephyr build that supports the built-in `openocd` runner.

Explicit runner selection is always available:

```sh
west flash -r openocd
west flash -r remote_openocd
west flash -r remote_openocd --remote lab
west debug -r remote_openocd
west attach -r remote_openocd
west debugserver -r remote_openocd
west rtt -r remote_openocd
west debug -r remote_openocd --rtt-server
west attach -r remote_openocd --rtt-server
west debugserver -r remote_openocd --rtt-server
```

`remote_openocd` reuses applicable behavior from Zephyr's built-in `openocd`
runner for the supported west workflows, but runs OpenOCD on the configured
remote host and sets the service and forwarding configuration for that
operation. `flash` creates no SSH forwards. `debug`, `attach`, and
`debugserver` forward GDB plus Tcl and telnet unless the corresponding runner
port option is `disabled`. The `rtt` command configures RTT through batch GDB,
changes GDB to best-effort, and then adds the required RTT forward before
launching the local channel-0 client. The three `--rtt-server` forms expose the
RTT endpoint with their configured initial forwards but do not launch a local
RTT client.

Service availability has command-specific requirements:

| Command | Required forwarding | Best-effort forwarding |
| --- | --- | --- |
| `debug`, `attach`, `debugserver` | GDB | Enabled Tcl and telnet |
| `debug --rtt-server`, `attach --rtt-server`, `debugserver --rtt-server` | GDB and RTT | Enabled Tcl and telnet |
| `rtt` | GDB during setup, then RTT | Enabled Tcl/telnet; GDB after setup |
| `flash` | None | None |

Best-effort forwards are attempted independently. Their occupied local ports,
startup failures, or later forwarding exits produce warnings while the required
interface remains usable. Runtime warnings appear at the next forwarding
status check; during interactive GDB this can be after GDB exits. Required
forwarding failure fails the operation. Explicitly requesting `--rtt-server`
requires RTT SSH forwarding at startup and throughout the operation. RTT
forwarding failure fails the command at the next status check; the runner does
not probe the remote RTT service. Failure to clean up an acquired SSH
process also fails the operation, including a best-effort attempt whose
rollback fails.

Forwarding follows the operation and runner port options; the runner does not
inspect arbitrary board or user Tcl to discover effective services.
The remote bind address and service-port settings are runner-owned transport
properties. Tcl that overrides `bindto`, `gdb_port`, `tcl_port`, `telnet_port`,
or another runner-owned service port is unsupported. An established local
forward confirms only that SSH accepted the forward; it does not guarantee
that OpenOCD has a listener behind it.

Direct semihosting uses ordinary user-supplied OpenOCD commands, typically
through `--cmd-pre-init`, and the existing OpenOCD stdout/stderr relay. It is
intentionally not a semihosting proxy, filesystem virtualization, TCP redirect,
or GDB File-I/O implementation. Remote OpenOCD may transparently pass GDB
File-I/O to a locally connected GDB, which may access the local GDB host's
filesystem. That behavior requires no runner involvement and is outside the
runner's compatibility guarantees.

## Operation recipes

### Flash

Build an image and run `west flash -r remote_openocd --remote lab`. This loads
the selected image on the target and should produce that application's normal
output. Use `west flash --context` first if you need to inspect available
runners.

### Debug

Run `west debug -r remote_openocd --remote lab`. The command starts remote
OpenOCD, loads the ELF, and starts local GDB. Set breakpoints and use GDB as
usual; the command cleans up the session when GDB exits. Debug does not promise
fresh application output.

### Attach

Run `west attach -r remote_openocd --remote lab` to connect GDB to the running
target without flashing or loading an image. Inspect the existing target state
and detach when finished.

### Debug server

Run `west debugserver -r remote_openocd --remote lab`. This starts remote
OpenOCD and forwards its GDB service, but does not start GDB. The runner prints
the local endpoint; connect a local GDB client using the printed address and
the build ELF, for example:

```text
target extended-remote 127.0.0.1:<printed-gdb-port>
```

The local port is `--gdb-client-port` (3333 by default); `--gdb-port` selects
the remote OpenOCD server port. For example,
`west debugserver -r remote_openocd --remote lab --gdb-client-port=3334`
prints `127.0.0.1:3334` as the GDB endpoint. Stop the debugserver process after
the client detaches.

### RTT

`west rtt -r remote_openocd --remote lab` requires an RTT-capable target and
configures channel 0 before launching the local RTT client. The command ends
when the client exits. With `--rtt-server`, for example
`west debugserver -r remote_openocd --remote lab --rtt-server`, the runner
prints successfully forwarded endpoints but leaves GDB and the RTT client to
the user. Both GDB and RTT SSH forwarding are required. RTT forwarding startup
failure aborts the command; runtime failure fails it at the next forwarding
status check. The runner does not probe the remote RTT service.
The RTT endpoint is a raw TCP channel at `127.0.0.1:5555` by default. Connect a
client such as `telnet 127.0.0.1 5555`, using the port printed by the runner.
Select another local and remote RTT port with `--rtt-port`, for example
`--rtt-port=5556`.

### Direct semihosting

For a target and OpenOCD configuration that support semihosting, pass the
ordinary OpenOCD setup commands through `--cmd-pre-init`, then run debug. A
representative invocation is:

```sh
west debug -r remote_openocd --remote lab \
  --cmd-pre-init='lappend post_init_commands {arm semihosting enable}' \
  --cmd-pre-init='lappend post_init_commands {arm semihosting_fileio disable}' \
  --cmd-pre-init='lappend post_init_commands {arm semihosting_redirect disable}'
```

The `lappend` form registers the commands before initialization and executes
them afterward, when the target is available. Semihosting text appears in the
relayed OpenOCD output. The runner provides no filesystem proxy or GDB File-I/O
configuration, virtualization, or path translation; the target, OpenOCD, and
GDB define that behavior. Other semihosting operations handled directly by
OpenOCD execute on the remote host according to OpenOCD behavior and are outside
the runner's compatibility guarantees.

## Stop and uninstall

Remote sessions relay OpenOCD output locally. If the local SSH client detects
loss, the runner fails the local operation and attempts local cleanup; the
remote helper cleans up OpenOCD after it observes control-channel loss. Stop
active west operations before removing the module.

Delete the module copy and, if it is no longer needed, the configuration created
by setup:

```sh
rm -rf "$HOME/zephyrproject/zephyr_remote_openocd"
rm -rf "$HOME/.config/zephyr_remote_openocd"
```

Remove the activation export from the current shell and from `~/.zephyrrc` if
you added it. After all sessions have stopped, these remote locations may also
be removed:

```text
~/.local/libexec/zephyr_remote_openocd/
$XDG_RUNTIME_DIR/zephyr_remote_openocd/
~/.cache/zephyr_remote_openocd/sessions/
```
