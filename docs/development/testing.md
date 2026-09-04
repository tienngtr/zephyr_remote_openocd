# Testing layers

The maintained suite uses pytest. Plain `pytest` runs only hardware-free unit
and local-process tests. External layers are selected explicitly so a normal
contributor run never needs SSH, a Zephyr checkout, or lab hardware:

```sh
pytest                         # unit + local integration
pytest tests/zephyr_integration -m zephyr
pytest tests/ssh_integration -m ssh --hardware-config /path/to/hardware.toml
pytest tests/hardware -m hardware --hardware-config /path/to/hardware.toml
```

## Common external-test setup

Create a test-only Python environment and install developer tools. This does
not install or package the production module:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

Copy [`tests/fixtures/hardware.example.toml`](../../tests/fixtures/hardware.example.toml)
to an ignored location, then replace its host, target, and tool placeholders.
The inventory must contain at least one host and one syntactically complete
target record, even for SSH-only tests; target build fields are not executed by
the SSH cases. Keep credentials, device paths, and lab identities outside Git.
The schema and capability profiles are documented in
[`hardware-fixtures.md`](hardware-fixtures.md).

Validate collection before execution:

```sh
.venv/bin/python -m pytest --collect-only -q \
  --hardware-config /path/to/hardware.toml
```

Use `ZRO_STRICT_EXTERNAL=1` when a missing prerequisite or unexpected skip
should fail the run. The SSH and hardware commands below use the same
`--hardware-config` option; `ZRO_HARDWARE_CONFIG` is an equivalent environment
override.

## Native Linux

Native SSH tests require an `ssh` executable on `PATH` and a reachable host
from the inventory. Zephyr integration additionally requires a Zephyr checkout,
the `west` executable used by that checkout, a supported Zephyr SDK/toolchain,
and PyYAML importable by the pytest Python environment (the Zephyr Python
environment normally provides it). Clean-install checks also require
`pyelftools` importable by the Python environment used by `west`. Hardware
tests also require the declared board, probe, serial endpoint, and remote
OpenOCD setup.

Run the desired layers explicitly:

```sh
ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  tests/ssh_integration -m ssh -k 'not wsl' \
  --hardware-config /path/to/hardware.toml

ZEPHYR_BASE=/path/to/zephyr \
WEST=/path/to/west \
OPENOCD_TEST_BOARD=<board> \
ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  tests/zephyr_integration -m zephyr

ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  tests/hardware -m hardware \
  --hardware-config /path/to/hardware.toml
```

## WSL 2

WSL cases use the same repository, test environment, and inventory setup. They
require an actual WSL 2 distribution and a reachable inventory host, but do
not require OpenOCD, a Zephyr checkout, or physical hardware. PG-012 tests the
WSL Linux `ssh` found on `PATH`; PG-013 tests Windows OpenSSH invoked from WSL.
These two tests use the inventory host address but select their SSH executables
themselves; the host's configured `ssh_command` is used by native-Linux SSH
tests.
Run the two cases independently:

```sh
# PG-012: WSL Linux OpenSSH (the normal WSL-side choice)
ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  tests/ssh_integration -m ssh -k 'test_wsl_linux_ssh' \
  --hardware-config /path/to/hardware.toml

# PG-013: optional Windows OpenSSH from WSL 2. Set ZRO_WINDOWS_SSH if needed.
export ZRO_WINDOWS_SSH=/mnt/c/Windows/System32/OpenSSH/ssh.exe
ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  tests/ssh_integration -m ssh -k 'test_windows_ssh_exe_from_wsl' \
  --hardware-config /path/to/hardware.toml
```

The tests detect WSL 2 from the kernel identity. On native Linux, the same
nodes skip by design; PG-012 and PG-013 remain deferred until this procedure
is run in WSL 2.

The strict release driver runs applicable layers serially and additionally
requires benchmark inputs:

```sh
python3 scripts/release_validate.py \
  --hardware-config /path/to/hardware.toml \
  --zephyr-base /path/to/zephyr \
  --west /path/to/west \
  --board <board> \
  --benchmark-build-dir /path/to/build \
  --benchmark-config /path/to/remote-openocd.toml \
  --benchmark-cwd /path/to/zephyr-workspace
```

PG-012 and PG-013 remain explicitly reported as deferred until a real WSL2
environment is available. The benchmark remains a manual, non-CI timing gate.

Run static checks for changed Python with `ruff check .` and
`ruff format --check .`; finish changes with `git diff --check`.
