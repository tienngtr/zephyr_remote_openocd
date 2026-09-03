# Tests

Run hardware-free unit tests:

```text
PYTHONPATH=python python3 -m unittest discover -s tests/unit -v
```

Run Zephyr 4.4 integration tests using any suitable OpenOCD-capable fixture board:

```text
PYTHONPATH=python \
ZEPHYR_BASE=/path/to/zephyr \
WEST=/path/to/west \
OPENOCD_TEST_BOARD=<board> \
python3 -m unittest tests.zephyr_integration.test_zephyr_integration -v
```

The Zephyr tests compile only `samples/hello_world`, use a harmless fake OpenOCD executable, and place all copied applications, caches, and build trees beneath ignored `.scratch/` directories.
They also include a clean-install acceptance test: it copies a Git-free module
tree to a temporary directory, uses an isolated `HOME`, runs setup with West's
Python environment, and activates only that copy through `EXTRA_ZEPHYR_MODULES`.

Run SSH integration tests:

```text
PYTHONPATH=python ZRO_SSH_TEST_HOST=<ssh-host-or-alias> \
python3 -m unittest tests.ssh_integration.test_ssh_integration -v
```

The SSH tests skip when `ZRO_SSH_TEST_HOST` is absent. The OpenOCD
configuration-consumption test additionally accepts `ZRO_SSH_TEST_OPENOCD` as
the absolute remote OpenOCD executable path, matching the production
`remote.openocd` setting. Alternatively, `ZRO_SSH_TEST_CONFIG` points to an
external TOML fixture and the test reads its configured `remote.host` and
`remote.openocd` values directly. PG-012 and PG-013 additionally require WSL 2; PG-013
accepts `ZRO_WINDOWS_SSH` as the path to Windows `ssh.exe`.

Real flash is an explicit, destructive hardware acceptance test. Set
`ZRO_REAL_FLASH_FIXTURES` to an external JSON fixture file. Each configured
target entry supplies `ssh_command`, `host`, `build_dir`, `config_path`,
`serial_device`, `serial_baud`, `expected_pattern`, and `serial_timeout`;
optional fields are `west`, `workspace`, `runner_args`, an `environment` table,
`expected_flash_patterns`, and
`assert_openocd_bindto`. When the latter is true, include a zero-argument
`--cmd-pre-init=bindto` so the test can compare OpenOCD's active bind name with
the helper allocation. The remote serial reader uses only Python's standard
library, configures raw 8N1, discards input until armed, and is test
infrastructure only. Device paths, baud rates, expected patterns, timeouts, and
other target-specific values belong in the external fixture and are not
committed to the repository.

The hardware acceptance coverage includes two board-agnostic fixture scenarios:

- explicit OpenOCD `--serial` selection together with one forwarded environment
  variable in the runner/helper process request;
- normal OpenOCD automatic probe selection without `--serial` when the remote
  host exposes one applicable probe.

Persistent debug acceptance is separately gated by `ZRO_REAL_DEBUG_FIXTURES`.
Each external fixture provides the same transport/build fields plus its local
GDB and ELF paths, debug runner arguments, enabled local service ports, serial
oracle, and a `supports_thread_info` capability. Every fixture runs debug,
attach, and debugserver. At least one capable fixture must also provide a
thread-info-enabled build and expected GDB thread pattern. All concrete hardware
identity and capability values remain outside the repository.

Real RTT acceptance is gated separately by `ZRO_REAL_RTT_FIXTURES`. Only entries
with `supports_rtt: true` run. Such entries provide `rtt_build_dir`,
`rtt_elf_file`, a non-default `rtt_port`, `expected_rtt_response`, and
optionally `rtt_input`, `rtt_timeout`, and
`rtt_runner_args`, in addition to the common remote-debug fixture fields. The
suite validates standalone bidirectional channel-0 I/O, simultaneous GDB and
RTT forwarding for `debug --rtt-server`, endpoint-only behavior for
`debugserver --rtt-server`, and cleanup. No local RTT client is expected for
either `--rtt-server` operation. All hardware identities and RTT capability
selection remain in the uncommitted fixture file.

Direct semihosting-console acceptance is gated by
`ZRO_REAL_SEMIHOSTING_FIXTURES`. Each entry supplies `supports_semihosting`,
the common remote/build fields, `semihosting_commands`, GDB control commands,
`expected_output`, and a timeout. Optional `runner_args` carry fixture-only
runner selections such as `--serial`. Semihosting commands are OpenOCD commands
passed through `--cmd-pre-init`; capable fixtures should register them with
`post_init_commands` so normal runner initialization precedes semihosting setup.
GDB commands are reserved for actions such as `continue` and orderly
halt/detach. The preferred fixture application exits or halts through
semihosting, but that behavior is not required when the test can perform
separate orderly termination after observing output.

The direct-mode fixture commands enable semihosting and disable both
`semihosting_fileio` and `semihosting_redirect`. Recording mode verifies the
generated invocation without I/O; hardware tests match fresh target text in
relayed OpenOCD stdout/stderr and check normal/interruption cleanup. At least
one capable fixture must pass, while unsupported combinations skip only this
capability. No semihosting proxy, filesystem virtualization, or GDB File-I/O
path is used, and board/device names remain in the external ignored fixture.
