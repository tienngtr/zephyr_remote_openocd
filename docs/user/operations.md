# Operations

Activate the module by appending its path to the semicolon-separated
`EXTRA_ZEPHYR_MODULES` list, for example:

```sh
export EXTRA_ZEPHYR_MODULES="${EXTRA_ZEPHYR_MODULES:+$EXTRA_ZEPHYR_MODULES;}/path/to/zephyr_remote_openocd"
```

For persistent activation, put the same export in `~/.zephyrrc`. Then build a
Zephyr application normally. The built-in `openocd` runner remains the default
unless `default_runner: remote_openocd` is selected.

For a concrete Zephyr 4.4 example, build the `samples/hello_world` application
for the OpenOCD-capable `stm32f746g_disco` board, inspect the available
runners, and select the `lab` remote from your configuration:

```sh
west build -p always -b stm32f746g_disco samples/hello_world
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
west debugserver -r remote_openocd --rtt-server
```

`remote_openocd` acts like Zephyr's built-in `openocd` runner for the supported
west workflows, but runs OpenOCD on the configured remote host and forwards its
enabled services to the local machine over SSH. Standalone `rtt` configures RTT
through batch GDB and launches the local channel-0 client. The two
`--rtt-server` forms expose the endpoint but do not launch a local RTT client.
Every enabled GDB, Tcl, and telnet service is forwarded; disabled services have
no local listener or readiness requirement.

Direct semihosting uses ordinary user-supplied OpenOCD commands, typically
through `--cmd-pre-init`, and the existing OpenOCD stdout/stderr relay. It is
intentionally not a semihosting proxy, filesystem virtualization, TCP redirect,
or GDB File-I/O implementation.

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

The local port follows the OpenOCD runner's configured GDB client port and may
be changed with the normal Zephyr runner option. Stop the debugserver process
after the client detaches.

### RTT

`west rtt -r remote_openocd --remote lab` requires an RTT-capable target and
configures channel 0 before launching the local RTT client. The command ends
when the client exits. With `--rtt-server`, for example
`west debugserver -r remote_openocd --remote lab --rtt-server`, the runner
prints both forwarded endpoints but leaves GDB and the RTT client to the user.

### Direct semihosting

For a target and OpenOCD configuration that support semihosting, pass the
ordinary OpenOCD setup commands through `--cmd-pre-init`, then run debug. A
representative invocation is:

```sh
west debug -r remote_openocd --remote lab \
  --cmd-pre-init='arm semihosting enable' \
  --cmd-pre-init='arm semihosting_fileio disable' \
  --cmd-pre-init='arm semihosting_redirect disable'
```

Semihosting text appears in the relayed OpenOCD output. The runner provides no
filesystem proxy or GDB File-I/O transport; the target and OpenOCD commands
must define the behavior.
