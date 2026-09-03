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

Run SSH integration tests:

```text
PYTHONPATH=python ZRO_SSH_TEST_HOST=<ssh-host-or-alias> \
python3 -m unittest tests.ssh_integration.test_ssh_integration -v
```

The SSH tests skip when `ZRO_SSH_TEST_HOST` is absent. PG-012 and PG-013 additionally require WSL 2; PG-013 accepts `ZRO_WINDOWS_SSH` as the path to Windows `ssh.exe`.

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
  variable;
- normal OpenOCD automatic probe selection without `--serial` when the remote
  host exposes one applicable probe.

Persistent debug acceptance is separately gated by `ZRO_REAL_DEBUG_FIXTURES`.
Each external fixture provides the same transport/build fields plus its local
GDB and ELF paths, debug runner arguments, enabled local service ports, serial
oracle, and a `supports_thread_info` capability. Every fixture runs debug,
attach, and debugserver. At least one capable fixture must also provide a
thread-info-enabled build and expected GDB thread pattern. All concrete hardware
identity and capability values remain outside the repository.
