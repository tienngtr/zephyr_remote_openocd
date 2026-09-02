# Operations

Activate the module by appending its path to the semicolon-separated
`EXTRA_ZEPHYR_MODULES` list, for example:

```sh
export EXTRA_ZEPHYR_MODULES="${EXTRA_ZEPHYR_MODULES:+$EXTRA_ZEPHYR_MODULES;}/path/to/zephyr_remote_openocd"
```

For persistent activation, put the same export in `~/.zephyrrc`. Then build a
Zephyr application normally. The built-in `openocd` runner remains the default
unless `runner.default = "remote_openocd"` is selected.

Explicit runner selection is always available:

```sh
west flash -r openocd
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
enabled services to the local machine over SSH. Standalone `rtt` configures RTT
through batch GDB and launches the local channel-0 client. The two
`--rtt-server` forms expose the endpoint but do not launch a local RTT client.
Every enabled GDB, Tcl, and telnet service is forwarded; disabled services have
no local listener or readiness requirement.

Direct semihosting uses ordinary user-supplied OpenOCD commands, typically
through `--cmd-pre-init`, and the existing OpenOCD stdout/stderr relay. It is
intentionally not a semihosting proxy, filesystem virtualization, TCP redirect,
or GDB File-I/O implementation.
