# Testing layers

Choose the smallest layer that covers the change:

- Every change: `pytest` and `python3 scripts/static_check.py`.
- Zephyr adapter or build integration: `tests/zephyr_integration/` with a
  Zephyr 4.4 source tree and its configured Python environment.
- SSH transport behavior: `tests/ssh_integration/` with an ignored inventory.
- Real board behavior: `tests/hardware/` with a board, probe, serial endpoint,
  and remote OpenOCD.
- Release evidence: the serial procedure in
  [`docs/validation/README.md`](../validation/README.md).

Run external nodes only after `--collect-only` confirms the intended selection.
Hardware and SSH tests can change external state; keep destructive profiles
serial and inspect cleanup output before reusing a target.

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

Test-owned pipe reads have deadlines, including partial-line and EOF waits.
The developer dependency `pytest-timeout` supplies a generous 30-minute
per-test backstop; operation-specific timeouts remain the primary diagnostics.
Tests clear product configuration/recording environment variables and set
their own values, independently of the developer's shell configuration.

Create the repository's test-only Python environment and install its developer
tools. This environment runs both self-contained and external pytest layers;
it does not install or package the production module:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements_dev.txt
```

Normal product use instead runs through Zephyr's configured Python environment.
External tests do not require pytest itself to run from that environment: they
use the Zephyr source, `west`, toolchain, and external fixtures required by the
selected layer. Running pytest from Zephyr's environment is also valid if that
interpreter has every dependency in `requirements_dev.txt`.

Copy [`tests/fixtures/hardware.example.toml`](../../tests/fixtures/hardware.example.toml)
to an ignored location, then replace its host, target, and tool placeholders.
The inventory must contain at least one host and one syntactically complete
target record, even for SSH-only tests; target build fields are not executed by
the SSH cases. Keep credentials, device paths, and lab identities outside Git.
The schema and capability profiles are documented in
[`hardware_fixtures.md`](hardware_fixtures.md).

Validate collection before execution:

```sh
.venv/bin/python -m pytest --collect-only -q \
  --hardware-config /path/to/hardware.toml
```

Use `ZRO_STRICT_EXTERNAL=1` when a missing prerequisite or unexpected skip
should fail the run. The SSH and hardware commands below use the same
`--hardware-config` option; `ZRO_HARDWARE_CONFIG` is an equivalent environment
override.

The `ZRO_` prefix identifies test and validation controls. Product settings use
the longer `ZEPHYR_REMOTE_OPENOCD_` prefix; for example,
`ZEPHYR_REMOTE_OPENOCD_CONFIG` selects the product YAML configuration, not this
test inventory.

## Runner recording mode

Set `ZRO_RECORD=1` to make `remote_openocd` construct the requested operation
and print its structured JSON record instead of executing it:

```sh
ZRO_RECORD=1 west flash -r remote_openocd --remote lab
```

The runner still loads product configuration and local build metadata needed to
construct the plan. It returns before starting SSH, OpenOCD, GDB, port
forwarding, or hardware access. Values other than the exact string `1` do not
enable recording.

Recording verifies configuration and command construction only. It does not
exercise packaged-helper discovery or deployment, remote connectivity, process
lifecycle and cleanup, target state, or hardware behavior. Do not use a
recording result as acceptance evidence for those behaviors.

For a thread-info-enabled `debug`, `attach`, `debugserver`, or `rtt` build, set
`ZRO_RECORD_VERSION` to representative output from `openocd --version`:

```sh
ZRO_RECORD=1 \
ZRO_RECORD_VERSION='Open On-Chip Debugger 0.12.0' \
west debug -r remote_openocd --remote lab
```

The injected version lets the runner construct its thread-awareness plan
without querying remote OpenOCD. It is ignored when thread information is not
requested and is not a substitute for testing the configured remote executable.

## Native Linux

The focused adapter contract tests need only the Zephyr source and its Python
runner dependencies, not a board, SDK, or firmware build:

```sh
ZEPHYR_BASE=/path/to/zephyr pytest tests/zephyr_integration/test_adapter.py
```

These compare applicable parser behavior with upstream and execute recording
through the real adapter while rejecting external process, socket, and SSH
operations. Static import-layer checks run in the ordinary unit suite.

Native SSH tests require an `ssh` executable on `PATH` and a reachable host
from the inventory. Zephyr integration additionally requires a Zephyr checkout,
the `west` executable used by that checkout, and a supported Zephyr
SDK/toolchain. PyYAML and jsonschema must be importable by the Python
environment running pytest. Clean-install checks also require `pyelftools` in
the Python environment used by `west`. Hardware tests require the declared
board, probe, serial endpoint, and remote OpenOCD setup.

Real `west debug` acceptance is source-level and architecture-independent. The
selected debug profile supplies an ELF and breakpoint symbol. GDB loads that
ELF, continues without a post-load reset, stops at the symbol, reads `$pc` and
the current instruction, then detaches. This deliberately does not use serial
output: RAM-loaded targets can lose the new image on reset, and Zephyr does not
promise fresh application output from `west debug`.

Real `west flash` acceptance uses two distinct images. It first flashes a quiet
precondition image (normally `samples/basic/minimal`) and confirms that the
selected image marker is absent, then flashes the selected image (normally
`samples/hello_world`) and requires its marker. Attach acceptance reads the PC
and current instruction without loading. RTT-server acceptance combines the
same source-level breakpoint evidence with a bidirectional RTT exchange.

Run the desired layers explicitly:

```sh
ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  tests/ssh_integration -m ssh -k 'not wsl' \
  --hardware-config /path/to/hardware.toml

ZEPHYR_BASE=/path/to/zephyr \
WEST=/path/to/west \
OPENOCD_TEST_BOARD=stm32f746g_disco \
ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  tests/zephyr_integration -m zephyr

ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  tests/hardware -m hardware \
  --hardware-config /path/to/hardware.toml
```

Select one real debug profile without running other destructive nodes by using
its complete parametrized node ID:

```sh
ZRO_STRICT_EXTERNAL=1 .venv/bin/python -m pytest \
  'tests/hardware/test_real_debug.py::TestRealOpenOcdDebug::test_debug[board:debug]' \
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
nodes skip by design. PG-012 and PG-013 validation remains pending because a
WSL 2 test environment is not currently available. This evidence gap does not
defer WSL 2 support.

Run all configured static checks with:

```sh
python3 scripts/static_check.py
```
