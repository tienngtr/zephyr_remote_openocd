# Operations

Use the `remote_openocd` runner with the usual west commands: `flash`, `debug`,
`attach`, `debugserver`, and `rtt`. It reuses the applicable behavior and
options of Zephyr's built-in `openocd` runner, but runs OpenOCD on the
configured remote host over SSH. West, GDB, and RTT clients stay on the local
machine, OpenOCD output appears in the local terminal, and OpenOCD services are
forwarded to local `127.0.0.1` ports. Any Zephyr 4.4.x build that supports the
built-in `openocd` runner can use `remote_openocd`.

## Requirements

Before using the commands on this page, make sure that:

- **`remote_openocd` is available in the build.** `west flash --context` must
  list it. If it does not, follow
  [activate the module](../../README.md#3-activate-the-module) and
  [configure the build](../../README.md#6-configure-the-build) in the README.
- **A remote is configured.** The configuration file defines the remote host
  and its OpenOCD command; see
  [Configure the remote](../../README.md#4-configure-the-remote) in the README.

## Runner, remote, and probe options

The examples on this page pass `-r remote_openocd --remote lab`, which selects
this runner and the remote named `lab`. Either option can be omitted:

- **Omit `--remote`** when `ZEPHYR_REMOTE_OPENOCD_REMOTE` or `default_remote`
  selects a remote; see
  [Remote selection order](configuration.md#remote-selection-order).
- **Omit `-r remote_openocd`** after setting `default_runner: remote_openocd`.
  In OpenOCD-capable builds, this setting overrides the board's default flash
  and debug runner, but not a `BOARD_FLASH_RUNNER` or `BOARD_DEBUG_RUNNER`
  override given when the build was configured; see
  [Choose the default remote and runner](configuration.md#choose-the-default-remote-and-runner).

Select any other available runner with `-r`, for example `west flash -r openocd`
for local OpenOCD.

On a remote host with several probes, pass `--serial` to select one.
`remote_openocd` applies it to every command, including `debugserver`, for
which the built-in `openocd` runner ignores it.

## Commands at a glance

| Task | Command | Local client |
| --- | --- | --- |
| [Program the target](#flash) | `west flash -r remote_openocd --remote lab` | None |
| [Load and debug the application](#debug) | `west debug -r remote_openocd --remote lab` | GDB |
| [Inspect the running target](#attach) | `west attach -r remote_openocd --remote lab` | GDB |
| [Connect a separately started GDB](#debug-server) | `west debugserver -r remote_openocd --remote lab` | None; GDB is started separately |
| [Open an RTT console](#rtt-console) | `west rtt -r remote_openocd --remote lab` | GDB for setup, then RTT client |
| [Add an RTT endpoint to a GDB session](#rtt-alongside-gdb) | Add `--rtt-server` to `debug`, `attach`, or `debugserver` | Same as without the option |
| [See semihosting output](#semihosting-output) | `west debug` with `--cmd-pre-init` setup commands | GDB |

## Flash

```sh
west flash -r remote_openocd --remote lab
```

The runner programs the selected image on the target through remote OpenOCD
and starts it. The application should then produce its normal output. A
remote OpenOCD flash failure fails the command.

With `--verify-only`, the runner verifies the image on the target instead of
programming it.

### Erase and verify options

Some erase and verify requests need an OpenOCD command for the image type.
Supply it with `--cmd-erase` or `--cmd-verify`, or let the board provide it:

| Request | Image | Required OpenOCD command |
| --- | --- | --- |
| `--erase` | HEX or BIN | Erase command (`--cmd-erase`) |
| `--verify` or `--verify-only` | BIN | Verify command (`--cmd-verify`) |

If the required command is missing, `remote_openocd` fails before contacting
the remote.

`remote_openocd` also fails before contacting the remote when `--erase` cannot
take effect:

- with `--verify-only`, for any image type, because a verify-only run does not
  erase;
- with an ELF image, even when an erase command is available, because ELF
  flashing loads the image into target memory without an erase step.

The image is ELF when `--file-type=elf`, a `--file` path ending in `.elf`, or a
board default such as `--use-elf` selects it. Use `--verify-only` alone to
check the image already on the target. To erase before programming, flash a
HEX or BIN image with `--erase`.

The built-in `openocd` runner does not fail in these cases:

- without an erase command, it logs an error and exits successfully without
  programming the target;
- without a verify command, it skips the verification;
- with `--erase` and `--verify-only`, or with `--erase` and an ELF image, it
  ignores `--erase`.

## Debug

```sh
west debug -r remote_openocd --remote lab
```

The runner starts remote OpenOCD and then local GDB, which loads the ELF. Set
breakpoints and use GDB as usual. Exit GDB to end the operation; the runner
then cleans up the remote session.

`remote_openocd` does not guarantee that the application produces fresh output
during `west debug`. To have the application start and produce its normal
output, run [`west flash`](#flash), which programs and starts the image.

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

Use `debugserver` to start GDB separately from the runner:

```sh
west debugserver -r remote_openocd --remote lab
```

The runner starts remote OpenOCD, forwards its GDB service, and prints the
local GDB endpoint as
`Remote OpenOCD GDB forwarding established at 127.0.0.1:PORT`. It does not
start GDB. Start a local GDB for the target
architecture with the build ELF, for example
`arm-zephyr-eabi-gdb build/zephyr/zephyr.elf` from the Zephyr SDK, and connect
it to the printed address, replacing `PORT` with the port from the printed
line:

```text
target extended-remote 127.0.0.1:PORT
```

After the client detaches, stop `debugserver` with Ctrl-C.

To use a different port, set `--gdb-port`. For example,
`west debugserver -r remote_openocd --remote lab --gdb-port=3334` prints
`127.0.0.1:3334` as the GDB endpoint. See [GDB ports](#gdb-ports) for how this
differs from the other commands.

## GDB ports

GDB uses local port 3333 by default. To use another port, for example 3334:

| Command | Options to set |
| --- | --- |
| `debugserver` | `--gdb-port=3334` |
| `debug`, `attach`, `rtt` | `--gdb-port=3334 --gdb-client-port=3334` |

The local port is always forwarded to the same port number on the remote host.

`--gdb-port` sets the OpenOCD base GDB port, and `--gdb-client-port` selects
the port that GDB connects to; both default to 3333. Their values can differ in
a multi-target OpenOCD configuration, where each target can have its own GDB
port: for example, `--gdb-port=3333 --gdb-client-port=3334` can connect `debug`
to the second target. `debugserver` does not start GDB, so it ignores
`--gdb-client-port`.

If an OpenOCD configuration file or command changes the GDB port, the runner
does not detect it and still forwards the port from its options; see
[Forwarded services](#forwarded-services).

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

`debug` and `attach` still start GDB, and `debugserver` still does not. No
local RTT client is started: the runner prints the forwarded RTT endpoint as
`Remote OpenOCD RTT forwarding established at 127.0.0.1:PORT`.
Connect any RTT client to the printed port, for example:

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

Semihosting text appears in the relayed OpenOCD output. The
`semihosting_fileio disable` and `semihosting_redirect disable` commands keep
semihosting I/O in OpenOCD rather than in GDB or a TCP connection. The
`lappend` form registers the commands before initialization and runs them
afterward, when the target is available.

> [!NOTE]
> Semihosting operations handled by OpenOCD, including file operations, run on
> the **remote host**. If remote OpenOCD instead passes GDB File-I/O through to
> a locally connected GDB, that GDB can access the **local** filesystem. The
> target, OpenOCD, and GDB define this behavior. `remote_openocd` does not proxy
> semihosting, virtualize the filesystem for semihosting or GDB File-I/O,
> forward a semihosting TCP redirect, or translate semihosting file paths. For
> semihosting, it guarantees only the console output described above.

## Stop or interrupt an operation

Each command normally ends as follows:

| Command | Ends when |
| --- | --- |
| `flash` | Flashing finishes |
| `debug`, `attach` | GDB exits |
| `rtt` | Ctrl-C is pressed, or remote OpenOCD exits |
| `debugserver` | Ctrl-C is pressed, or remote OpenOCD exits |

To stop an operation, press Ctrl-C once and wait for cleanup to finish.
Pressing Ctrl-C again does not skip or shorten the cleanup; let it finish so
that remote OpenOCD releases the probe.

Once a remote session exists, the runner prints
`Interrupted; cleaning up the remote OpenOCD session`, stops OpenOCD and its
SSH transports, and then exits as an interrupted command. If the operation had
already failed, that failure is reported instead.

Because `rtt` and `debugserver` normally end with Ctrl-C, they also exit as
interrupted commands, not successfully.

Ctrl-C takes effect during the operation, with these exceptions:

- **During local preparation**, such as packaging build files to copy to the
  remote host, Ctrl-C takes effect when that step finishes.
- **Inside interactive GDB** for `debug` or `attach`, Ctrl-C belongs to GDB.
  Exit GDB to end the operation.

## Uninstall

1. Stop all active west operations that use the module.
2. Remove the `EXTRA_ZEPHYR_MODULES` activation export, and any
   `ZEPHYR_REMOTE_OPENOCD_CONFIG` export, from the current shell and from
   `~/.zephyrrc` if they were added there.
3. Delete the module directory and, if the configuration is no longer needed,
   the configuration directory that the setup script created. A configuration
   file that the setup script created at another path is not in that
   directory; delete it separately. The first
   command uses the example module path from the README's
   [Obtain the module](../../README.md#1-obtain-the-module) step; replace it
   with the path where the module was placed:

   ```sh
   rm -rf "$HOME/zephyrproject/zephyr_remote_openocd"
   rm -rf "$HOME/.config/zephyr_remote_openocd"
   ```

4. Optionally delete the local preferred-address cache at
   `~/.cache/zephyr_remote_openocd/preferred-addresses/`. This removes only
   remembered addresses, not SSH forwards retained by an
   [SSH connection-sharing](#ssh-connection-sharing) master; to remove those,
   see
   [A retained forward occupies the local port](troubleshooting.md#a-retained-forward-occupies-the-local-port).
5. After all sessions have stopped, optionally remove these locations on the
   remote host. Before removing session workspaces, follow
   [Retained session data](troubleshooting.md#retained-session-data):

   ```text
   ~/.local/libexec/zephyr_remote_openocd/
   $XDG_RUNTIME_DIR/zephyr_remote_openocd-sessions-v1/
   ~/.cache/zephyr_remote_openocd/sessions-v1/
   ```

## Reference

### Forwarded services

| Command | Required forwarding | Best-effort forwarding |
| --- | --- | --- |
| `debug`, `attach`, `debugserver` | GDB | Enabled Tcl server and telnet |
| `debug --rtt-server`, `attach --rtt-server`, `debugserver --rtt-server` | GDB and RTT | Enabled Tcl server and telnet |
| `rtt` | GDB during setup, then RTT | Enabled Tcl server and telnet; GDB after setup |
| `flash` | None | None |

**Which services are forwarded.** The OpenOCD Tcl server is forwarded unless
`--tcl-port` is `disabled`, and telnet unless `--telnet-port` is `disabled`.
Forwarding follows the command and runner port options; the runner does not
read OpenOCD configuration files or commands to discover which services are
actually enabled.

**Port numbers.** Each forwarded service uses the same port number locally and
on the remote host: GDB at `--gdb-client-port` for `debug`, `attach`, and `rtt`
and at `--gdb-port` for `debugserver`, the OpenOCD Tcl server at `--tcl-port`,
telnet at `--telnet-port`, and RTT at `--rtt-port`. To move a service to
another port, use these options; for GDB with `debug`, `attach`, or `rtt`, set
both GDB port options as shown in [GDB ports](#gdb-ports).

**How `rtt` changes forwarding.** The runner first configures RTT through batch
GDB. It then makes GDB best-effort and adds the required RTT forward before
starting the local channel-0 client.

**Required forwards.** If a required forward fails, the operation fails. An
explicit `--rtt-server` makes RTT forwarding required from startup through the
end of the operation: a startup failure aborts the command, and a later failure
fails it at the runner's next status check, which can be after GDB exits. The
runner does not probe the remote RTT service.

**Best-effort forwards.** Each is attempted independently. An occupied local
port, a startup failure, or a later forwarding exit produces a warning, and the
required service remains usable. Runtime warnings appear at the runner's next
status check; during interactive GDB, this can be after GDB exits.

**Cleanup failures are fatal.** If the runner cannot clean up an SSH process it
started, the operation fails, even when that process belonged to a failed
best-effort forwarding attempt.

**`remote_openocd` does not support OpenOCD configuration that overrides its
ports or bind address.** The runner binds remote OpenOCD services to a loopback
address allocated for the session and sets their ports from its port options.
Board or user OpenOCD configuration files or commands can still override
`bindto`, `gdb_port`, `tcl_port`, `telnet_port`, or another service port that
the runner sets. `remote_openocd` does not detect such overrides, still
forwards the ports from its options, and does not guarantee correct operation
of a session with such an override.

**An established forward is not a listener.** A local forward confirms only
that SSH accepted it, not that OpenOCD is listening behind it.

### Exit status and connection loss

When the operation completes, the runner asks the remote helper to clean up
while it keeps reading final output and results:

- **Stopping OpenOCD to end the session does not fail successful work.** For
  example, when GDB exits after a successful `debug` session, the runner stops
  OpenOCD, and that stop alone does not fail the command.
- **An earlier failure stays primary.** Later shutdown problems do not replace
  a failure the runner had already recorded.
- **OpenOCD may finish first.** If OpenOCD exits on its own before the cleanup
  request, its observed result is kept, so a failing exit still fails the
  operation.
- **Unconfirmed results fail.** A missing final result or unconfirmed remote
  cleanup still fails the operation; see
  [shutdown troubleshooting](troubleshooting.md#shutdown-results).

If the local SSH client detects a lost connection, the runner fails the
operation and attempts local cleanup at the runner's next status check, which
can be after GDB exits. The remote helper begins a bounded cleanup of OpenOCD
and the session only after it observes that its control channel is gone; local
detection does not show when that happens. See
[The SSH connection is lost](troubleshooting.md#the-ssh-connection-is-lost).

### SSH connection sharing

With OpenSSH `ControlMaster` or another SSH connection-sharing mechanism, local
forwards can outlive the runner command, and a stale forward can cause port
conflicts that must be cleaned up through the SSH client. Sharing state stays
under the SSH client's control. To run concurrent sessions over a shared
connection, see [Connection sharing](configuration.md#connection-sharing); for
problems, see
[SSH connection sharing](troubleshooting.md#ssh-connection-sharing).

When a session closes, the runner attempts to clean up what it started: its SSH
subprocesses and their I/O, remote OpenOCD, the session's reserved remote
address, and the remote session workspace. Forwards kept by an external sharing
master are not included and may remain after those subprocesses exit.

To keep retained forwards usable, the runner asks to reuse a remote address
remembered from an earlier session. It can receive a different address, for
example when another session holds that address or its remote port is
occupied. A forward retained from the earlier session can then occupy the
local port; see
[A retained forward occupies the local port](troubleshooting.md#a-retained-forward-occupies-the-local-port).
