# Operations

Activate the module by appending its path to the semicolon-separated
`EXTRA_ZEPHYR_MODULES` list, for example:

```sh
export EXTRA_ZEPHYR_MODULES="${EXTRA_ZEPHYR_MODULES:+$EXTRA_ZEPHYR_MODULES;}/path/to/zephyr_remote_openocd"
```

For shell activation in every Zephyr session, put the same export in
`~/.zephyrrc`. Then build a
Zephyr application normally. For OpenOCD-capable builds, `default_runner`
selects the default flash and debug runner, even when the board's original
default is another runner; select any other available runner with `-r`.

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

SSH connection-sharing state remains externally managed. On close, the runner
cleans up the SSH subprocesses and I/O resources it launched, remote OpenOCD,
its helper lease, and its workspace. An external sharing master may retain
local forwards after those subprocesses exit. The runner offers a remembered
remote address to improve reuse of matching retained forwards, but the helper
can reject the preference because of an active lease or occupied remote port.
Stale retained forwards may then require cleanup through your SSH client; see
[connection-sharing troubleshooting](troubleshooting.md#ssh-connection-sharing).

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

Unlike the built-in `openocd` runner, `remote_openocd` fails instead of
reporting success when it cannot perform a requested step: `--erase` on a HEX
or BIN image needs an erase command (`--cmd-erase`), and `--verify` or
`--verify-only` on a BIN image needs a verify command (`--cmd-verify`), whether
supplied on the command line or by the board. Likewise, `debugserver` honors
`--serial`, which the built-in runner ignores for that command, so the
requested probe is selected on a remote host with several probes.

### Debug

Run `west debug -r remote_openocd --remote lab`. The command starts remote
OpenOCD, loads the ELF, and starts local GDB. Set breakpoints and use GDB as
usual; the command cleans up the session when GDB exits. Debug does not promise
fresh application output. Ctrl-C inside interactive GDB remains debugger
interaction; exit GDB to end the runner operation.

### Attach

Run `west attach -r remote_openocd --remote lab` to connect GDB to the running
target without flashing or loading an image. Inspect the existing target state
and detach when finished. Interactive GDB has the same shutdown behavior as debug.

### Debug server

Run `west debugserver -r remote_openocd --remote lab`. This starts remote
OpenOCD and forwards its GDB service, but does not start GDB. The runner prints
the local endpoint; connect a local GDB client using the printed address and
the build ELF, for example:

```text
target extended-remote 127.0.0.1:<printed-gdb-port>
```

For `debugserver`, `--gdb-port` selects both the remote OpenOCD base listener
and its local forwarded port (3333 by default); `--gdb-client-port` has no effect.
For example,
`west debugserver -r remote_openocd --remote lab --gdb-port=3334`
prints `127.0.0.1:3334` as the GDB endpoint. Stop the debugserver process after
the client detaches.

For `debug`, `attach`, and `rtt`, `--gdb-port` still configures the OpenOCD base
listener, while `--gdb-client-port` selects the target listener GDB connects to.
That selected port is forwarded locally to the same remote port. For example,
`--gdb-port=3333 --gdb-client-port=3334` can select the second target in a
multi-target OpenOCD configuration. To move a single-target session to port 3334,
set both options to 3334.

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

### Interrupting an operation

Ctrl-C cancels a runner operation while it prepares the remote session, waits
for OpenOCD, forwards services, flashes, serves `debugserver`, or relays RTT.
Once a remote session exists, the runner prints
`Interrupted; cleaning up the remote OpenOCD session` and stops OpenOCD and its
SSH transports before exiting as an interrupted command. Pressing Ctrl-C again
does not skip or shorten that bounded cleanup, which keeps the probe from being
left in use. A Ctrl-C pressed during local work, such as packaging build files
for staging, takes effect when that work finishes. Inside interactive GDB for
`debug` or `attach`, Ctrl-C belongs to GDB; exit GDB to end the operation.

## Stop and uninstall

Remote sessions relay OpenOCD output locally. If the local SSH client detects
loss, the runner fails the local operation and attempts local cleanup; the
remote helper cleans up OpenOCD after it observes control-channel loss. Stop
active west operations before removing the module.

On normal completion, the runner requests remote cleanup while continuing to
read final output and results. OpenOCD may exit naturally before that request
arrives; its observed result is retained, and later shutdown failures do not
replace an already-established operation failure. Termination performed solely
to dispose of a live server does not turn successful local client work into a
failure. Missing final results or unconfirmed remote disposal still fail the
operation; see [shutdown troubleshooting](troubleshooting.md#shutdown-results).

Delete the module copy and, if it is no longer needed, the configuration created
by setup:

```sh
rm -rf "$HOME/zephyrproject/zephyr_remote_openocd"
rm -rf "$HOME/.config/zephyr_remote_openocd"
```

Remove the activation export from the current shell and from `~/.zephyrrc` if
you added it. The local preferred address cache may also be deleted at
`~/.cache/zephyr_remote_openocd/preferred-addresses/`; this only removes cached
preferred addresses, not externally retained SSH forwards. After all sessions
have stopped, these remote locations may also be removed:

```text
~/.local/libexec/zephyr_remote_openocd/
$XDG_RUNTIME_DIR/zephyr_remote_openocd-sessions-v1/
~/.cache/zephyr_remote_openocd/sessions-v1/
```
