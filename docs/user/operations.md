# Operations

Use the `remote_openocd` runner with the usual west commands: `flash`, `debug`,
`attach`, `debugserver`, and `rtt`. It reuses the applicable behavior and
options of Zephyr's built-in `openocd` runner, but runs OpenOCD on the
configured remote host over SSH. West, GDB, and RTT clients stay on your
machine, OpenOCD output appears in your terminal, and OpenOCD services are
forwarded to local `127.0.0.1` ports. Any Zephyr 4.4.x build that supports the
built-in `openocd` runner can use `remote_openocd`.

## Before you start

- **The module is active in the build.** `west flash --context` must list
  `remote_openocd`. If it does not, follow
  [activate the module](../../README.md#3-activate-the-module) and
  [configure the build](../../README.md#6-configure-the-build) in the README.
- **A remote is configured.** The examples on this page select the `lab`
  remote with `--remote lab`. You can omit `--remote` when
  `ZEPHYR_REMOTE_OPENOCD_REMOTE` or `default_remote` selects a remote; see
  [defaults and inheritance](configuration.md#defaults-and-inheritance).
- **The runner is selected.** The examples pass `-r remote_openocd`. To omit
  it, set `default_runner: remote_openocd`; see the
  [configuration quick start](configuration.md#quick-start). For
  OpenOCD-capable builds, `default_runner` selects either `openocd` or
  `remote_openocd` as the default flash and debug runner, even when the
  board's own default is another runner. Select any other available runner
  with `-r`, for example `west flash -r openocd` for local OpenOCD.
- **Board and user Tcl leave service ports to the runner.** The runner sets the
  remote bind address and OpenOCD service ports. Tcl that overrides them is
  unsupported; see [Forwarded services](#forwarded-services).

## Choose a command

| Task | Command | Local client |
| --- | --- | --- |
| [Program the target](#flash) | `west flash -r remote_openocd --remote lab` | None |
| [Load and debug the application](#debug) | `west debug -r remote_openocd --remote lab` | GDB |
| [Inspect the running target](#attach) | `west attach -r remote_openocd --remote lab` | GDB |
| [Serve GDB to your own client](#debug-server) | `west debugserver -r remote_openocd --remote lab` | None; connect your own |
| [Open an RTT console](#rtt-console) | `west rtt -r remote_openocd --remote lab` | GDB for setup, then RTT client |
| [Add an RTT endpoint to a GDB session](#rtt-alongside-gdb) | Add `--rtt-server` to `debug`, `attach`, or `debugserver` | Same as without the option |
| [See semihosting output](#semihosting-output) | `west debug` with `--cmd-pre-init` setup commands | GDB |

## Flash

```sh
west flash -r remote_openocd --remote lab
```

The runner programs the selected image on the target through remote OpenOCD
and starts it. The application should then produce its normal output. With
`--verify-only`, the runner verifies the image on the target instead of
programming it. A remote OpenOCD flash failure fails the command.

### Erase and verify options

`--erase` cannot be combined with `--verify-only`, because a verify-only run
never erases. `remote_openocd` rejects that combination for any image type.

Some erase and verify requests need an OpenOCD command for the image type.
Supply it with `--cmd-erase` or `--cmd-verify`, or let the board provide it:

| Request | Image | Required OpenOCD command |
| --- | --- | --- |
| `--erase` | HEX or BIN | Erase command (`--cmd-erase`) |
| `--verify` or `--verify-only` | BIN | Verify command (`--cmd-verify`) |

In each of these cases, `remote_openocd` fails before contacting the remote.
The built-in `openocd` runner instead reports success: it ignores `--erase`
with `--verify-only`, skips a verification that has no verify command, and
without an erase command does not program the target at all.

## Debug

```sh
west debug -r remote_openocd --remote lab
```

The runner starts remote OpenOCD and then local GDB, which loads the ELF. Set
breakpoints and use GDB as usual. Exit GDB to end the operation; the runner
then cleans up the remote session.

`west debug` does not promise fresh application output.

Ctrl-C inside GDB is handled by GDB as usual and does not end the runner
operation; see [Stop or interrupt an operation](#stop-or-interrupt-an-operation).

## Attach

```sh
west attach -r remote_openocd --remote lab
```

GDB connects to the running target without flashing or loading an image.
Inspect the existing target state and detach when finished. Exiting GDB ends
the operation, and Ctrl-C inside GDB works as it does for [debug](#debug).

## Debug server

Use `debugserver` to connect your own GDB client or IDE:

```sh
west debugserver -r remote_openocd --remote lab
```

The runner starts remote OpenOCD, forwards its GDB service, and prints the
local GDB endpoint. It does not start GDB. Start a local GDB client with the
build ELF, for example `<gdb> build/zephyr/zephyr.elf`, and connect it to the
printed address:

```text
target extended-remote 127.0.0.1:<printed-gdb-port>
```

After the client detaches, stop `debugserver` with Ctrl-C.

To use a different port, set `--gdb-port`. For example,
`west debugserver -r remote_openocd --remote lab --gdb-port=3334` prints
`127.0.0.1:3334` as the GDB endpoint. See [GDB ports](#gdb-ports) for how this
differs from the other commands.

On a remote host with several probes, pass `--serial` to request a specific
probe. Unlike the built-in `openocd` runner, which ignores `--serial` for
`debugserver`, `remote_openocd` applies it as it does for the other commands.

## GDB ports

To move a single-target `debug`, `attach`, or `rtt` session to port 3334, set
both `--gdb-port=3334` and `--gdb-client-port=3334`. For `debugserver`,
`--gdb-port=3334` alone is enough.

The two options do different jobs. `--gdb-port` sets the remote OpenOCD
GDB-server base port. `--gdb-client-port` selects which OpenOCD GDB port GDB
connects to. Both default to 3333. The local GDB port depends on the command:

| Command | Local GDB port | `--gdb-client-port` |
| --- | --- | --- |
| `debugserver` | Same as `--gdb-port` | Has no effect |
| `debug`, `attach`, `rtt` (GDB setup) | Same as `--gdb-client-port`, forwarded to the same remote port | Selects the port GDB connects to |

For example, with `debug`, `--gdb-port=3333 --gdb-client-port=3334` can select
the second target of a multi-target OpenOCD configuration.

## RTT

RTT channel 0 is available as a raw TCP endpoint, at `127.0.0.1:5555` by
default. `--rtt-port` selects both the local and the remote RTT port, for
example `--rtt-port=5556`.

### RTT console

The target must be RTT-capable. Run:

```sh
west rtt -r remote_openocd --remote lab
```

The runner configures RTT channel 0 through remote OpenOCD, using local GDB in
batch mode, and then starts the local RTT client. Press Ctrl-C to end the
session; it also ends if remote OpenOCD exits. If the RTT connection closes
while remote OpenOCD is still running, the command fails.

### RTT alongside GDB

Add `--rtt-server` to `debug`, `attach`, or `debugserver` to expose the RTT
endpoint during that session:

```sh
west debugserver -r remote_openocd --remote lab --rtt-server
```

`debug` and `attach` still start GDB, and `debugserver` still leaves GDB to you.
No local RTT client is started: the runner prints the forwarded RTT endpoint,
and you connect a client of your choice to the printed port, for example:

```sh
telnet 127.0.0.1 5555
```

GDB and RTT forwarding are both required for the whole session: if either SSH
forward fails, the command fails. The runner does not check that the remote RTT
service is responding; see [Forwarded services](#forwarded-services).

## Semihosting output

For a target and OpenOCD configuration that support semihosting, pass the
ordinary OpenOCD setup commands through `--cmd-pre-init` and run `debug`. For
example:

```sh
west debug -r remote_openocd --remote lab \
  --cmd-pre-init='lappend post_init_commands {arm semihosting enable}' \
  --cmd-pre-init='lappend post_init_commands {arm semihosting_fileio disable}' \
  --cmd-pre-init='lappend post_init_commands {arm semihosting_redirect disable}'
```

Semihosting text appears in the relayed OpenOCD output. The `lappend` form
registers the commands before initialization and runs them afterward, when the
target is available.

> [!NOTE]
> Semihosting operations handled by OpenOCD, including file operations, run on
> the **remote host**. If remote OpenOCD instead passes GDB File-I/O through to
> a locally connected GDB, that GDB can access your **local** filesystem. The
> target, OpenOCD, and GDB define this behavior without runner involvement: the
> runner provides no semihosting proxy, filesystem virtualization, TCP
> redirect, or path translation. Only the console output above is within the
> runner's compatibility guarantees.

## Stop or interrupt an operation

Each command normally ends as follows:

| Command | Ends when |
| --- | --- |
| `flash` | Flashing finishes |
| `debug`, `attach` | You exit GDB |
| `rtt` | You press Ctrl-C, or remote OpenOCD exits |
| `debugserver` | You press Ctrl-C, or remote OpenOCD exits |

To stop an operation, press Ctrl-C once and wait. Once a remote session
exists, the runner prints
`Interrupted; cleaning up the remote OpenOCD session`, stops OpenOCD and its
SSH transports, and then exits as an interrupted command. If the operation had
already failed, that failure is reported instead. This also applies when Ctrl-C
ends `rtt` or `debugserver` normally: the command reports an interruption, not
success.

Ctrl-C takes effect while the runner prepares the remote session, waits for
OpenOCD, forwards services, flashes, serves `debugserver`, or relays RTT, with
these exceptions:

- **During local work**, such as packaging build files for staging, Ctrl-C
  takes effect when that work finishes.
- **Inside interactive GDB** for `debug` or `attach`, Ctrl-C belongs to GDB.
  Exit GDB to end the operation.

Pressing Ctrl-C again does not skip or shorten the cleanup. Let it finish so
that remote OpenOCD releases the probe.

## Uninstall

1. Stop all active west operations that use the module.
2. Remove the `EXTRA_ZEPHYR_MODULES` activation export from the current shell
   and from `~/.zephyrrc` if you added it there.
3. Delete the module copy and, if you no longer need it, the configuration
   created by setup. Substitute the path where you placed the module:

   ```sh
   rm -rf "$HOME/zephyrproject/zephyr_remote_openocd"
   rm -rf "$HOME/.config/zephyr_remote_openocd"
   ```

4. Optionally delete the local preferred-address cache at
   `~/.cache/zephyr_remote_openocd/preferred-addresses/`. This removes only
   remembered addresses, not SSH forwards retained by a sharing master.
5. After all sessions have stopped, you may remove these locations on the
   remote host:

   ```text
   ~/.local/libexec/zephyr_remote_openocd/
   $XDG_RUNTIME_DIR/zephyr_remote_openocd-sessions-v1/
   ~/.cache/zephyr_remote_openocd/sessions-v1/
   ```

## Reference

### Forwarded services

| Command | Required forwarding | Best-effort forwarding |
| --- | --- | --- |
| `debug`, `attach`, `debugserver` | GDB | Enabled Tcl and telnet |
| `debug --rtt-server`, `attach --rtt-server`, `debugserver --rtt-server` | GDB and RTT | Enabled Tcl and telnet |
| `rtt` | GDB during setup, then RTT | Enabled Tcl and telnet; GDB after setup |
| `flash` | None | None |

**Which services are forwarded.** Tcl is forwarded unless `--tcl-port` is
`disabled`, and telnet unless `--telnet-port` is `disabled`. Forwarding follows
the command and runner port options; the runner does not inspect board or user
Tcl to discover which services are actually enabled.

**How `rtt` changes forwarding.** The runner first configures RTT through batch
GDB. It then makes GDB best-effort and adds the required RTT forward before
starting the local channel-0 client.

**Required forwards.** If a required forward fails, the operation fails. An
explicit `--rtt-server` makes RTT forwarding required from startup through the
end of the operation: a startup failure aborts the command, and a later failure
fails it at the next forwarding status check. The runner does not probe the
remote RTT service.

**Best-effort forwards.** Each is attempted independently. An occupied local
port, a startup failure, or a later forwarding exit produces a warning, and the
required service remains usable. Runtime warnings appear at the next
forwarding status check; during interactive GDB, this can be after GDB exits.

**Cleanup failures are fatal.** If the runner cannot clean up an SSH process it
started, the operation fails, even when that process belonged to a failed
best-effort forwarding attempt.

**Ports and bind addresses belong to the runner.** The runner sets the remote
bind address and service ports. Board or user Tcl that overrides `bindto`,
`gdb_port`, `tcl_port`, `telnet_port`, or another runner-owned service port is
unsupported.

**An established forward is not a listener.** A local forward confirms only
that SSH accepted it, not that OpenOCD is listening behind it.

### Exit status and connection loss

When the operation completes, the runner asks the remote side to clean up while
it keeps reading final output and results:

- **Disposing of the server does not fail successful work.** Stopping a live
  OpenOCD server only to end the session does not turn successful client work
  into a failure.
- **An earlier failure stays primary.** Later shutdown problems do not replace
  a failure the runner had already recorded.
- **OpenOCD may finish first.** If OpenOCD exits on its own before the cleanup
  request, its observed result is kept.
- **Unconfirmed results fail.** A missing final result or unconfirmed remote
  cleanup still fails the operation; see
  [shutdown troubleshooting](troubleshooting.md#shutdown-results).

If the local SSH client detects a lost connection, the runner fails the
operation and attempts local cleanup. The remote helper cleans up OpenOCD after
it observes that its control channel is gone.

### SSH connection sharing

If you use OpenSSH `ControlMaster` or another SSH connection-sharing mechanism,
local forwards can outlive the runner command, and a stale forward can cause
port conflicts that you clean up through your SSH client. Sharing state stays
under your SSH client's control; see
[connection-sharing troubleshooting](troubleshooting.md#ssh-connection-sharing),
including how to run concurrent sessions over a shared connection.

When a session closes, the runner cleans up what it started: its SSH
subprocesses and their I/O, remote OpenOCD, the session's reserved remote
address, and the remote session workspace. Forwards kept by an external sharing
master are not included and may remain after those subprocesses exit.

To keep retained forwards usable, the runner asks to reuse a remote address
remembered from an earlier session. It can receive a different address, for
example when another session holds that address or its remote port is
occupied.
