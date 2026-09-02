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

