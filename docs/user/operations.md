# Operations

Activate the module with `EXTRA_ZEPHYR_MODULES=/path/to/zephyr-remote-openocd`
and build a Zephyr application normally. The built-in `openocd` runner remains
the local default unless `[zephyr].default = "remote"` is selected.

Explicit runner selection is always available:

```sh
west flash -d build -r openocd
west flash -d build -r remote-openocd
west debug -d build -r remote-openocd
west attach -d build -r remote-openocd
west debugserver -d build -r remote-openocd
west rtt -d build -r remote-openocd
west debug -d build -r remote-openocd --rtt-server
west debugserver -d build -r remote-openocd --rtt-server
```

`debug` preserves Zephyr's load and halt behavior; `attach` connects without
loading; and `debugserver` keeps the remote session alive without launching a
local GDB. Standalone `rtt` configures RTT through batch GDB and launches the
local channel-0 client. The two `--rtt-server` forms expose the endpoint but do
not launch a local RTT client. Every enabled GDB, Tcl, and telnet service is
forwarded; disabled services have no local listener or readiness requirement.

Direct semihosting uses fixture/application-supplied OpenOCD commands and the
existing OpenOCD stdout/stderr relay. It is intentionally not a semihosting
proxy, filesystem virtualization, TCP redirect, or GDB File-I/O implementation.
