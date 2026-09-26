# Testing layers

The contributor-facing validation policy is summarized in
[`../../CONTRIBUTING.md`](../../CONTRIBUTING.md). This page provides the
layer-specific commands and external-test setup details.

Choose the smallest layer that covers the change:

- Every change: `.venv/bin/python -m pytest` and `.venv/bin/python scripts/contributor/static_check.py`.
- Zephyr adapter or build integration: `tests/zephyr_integration/` with a
  Zephyr 4.4 source tree and its configured Python environment.
- SSH transport behavior: `tests/ssh_integration/` with a local hardware inventory.
- Real board behavior: `tests/hardware/` with a board, probe, serial endpoint,
  and remote OpenOCD.

Run external nodes only after `--collect-only` confirms the intended selection.
Hardware and SSH tests can change external state; keep destructive profiles
serial and inspect cleanup output before reusing a target.

The maintained suite uses pytest. The ordinary `.venv/bin/python -m pytest`
command runs only hardware-free unit and local-process tests. GitHub Actions
runs four independent validation jobs: the self-contained suite, static
checks, the SSH suite against an isolated SSH server container, and the full
Zephyr 4.4 integration suite with a real workspace and SDK. That suite runs
real Zephyr CMake configuration and west runner commands in recording mode;
it does not compile or execute firmware. The SSH server has its own filesystem,
process namespace, and network namespace; the tests use the real SSH client
and server. External layers remain explicit locally, so a normal contributor
run never needs SSH, a Zephyr checkout, or lab hardware:

```sh
.venv/bin/python -m pytest
.venv/bin/python -m pytest tests/zephyr_integration -m zephyr
.venv/bin/python -m pytest tests/ssh_integration -m ssh --hardware-inventory /path/to/hardware.yaml
.venv/bin/python -m pytest tests/hardware -m hardware --hardware-inventory /path/to/hardware.yaml
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
use the Zephyr source, `west`, toolchain, and external test resources required by the
selected layer. Running pytest from Zephyr's environment is also valid if that
interpreter has every dependency in `requirements_dev.txt`.

Copy [`tests/fixtures/hardware.example.yaml`](../../tests/fixtures/hardware.example.yaml)
to `.scratch/config/hardware.yaml`, then replace its host, target, and tool
placeholders. The repository excludes `.scratch/` from version control.
The inventory must contain at least one host and one syntactically complete
target record, even for SSH-only tests; target build fields are not executed by
the SSH cases. Keep credentials, device paths, and lab identities outside Git.
The schema and capability profiles are documented in
[`hardware_inventories.md`](hardware_inventories.md).

Validate the inventory without external I/O before collection:

```sh
.venv/bin/python scripts/contributor/validate_hardware_inventory.py \
  .scratch/config/hardware.yaml --check-local
```

Validate collection before execution:

```sh
.venv/bin/python -m pytest --collect-only -q \
  --hardware-inventory /path/to/hardware.yaml
```

Use `--require-external-tests` when a missing prerequisite or unexpected skip
should fail the run. The SSH and hardware commands below use the same
`--hardware-inventory` option.

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
`ZRO_RECORD_OPENOCD_VERSION` to representative output from `openocd --version`:

```sh
ZRO_RECORD=1 \
ZRO_RECORD_OPENOCD_VERSION='Open On-Chip Debugger 0.12.0' \
west debug -r remote_openocd --remote lab
```

The injected version lets the runner construct its thread-awareness plan
without querying remote OpenOCD. It is ignored when thread information is not
requested and is not a substitute for testing the configured remote executable.

## Linux

The focused adapter contract tests need only the Zephyr source and its Python
runner dependencies, not a board, SDK, or firmware build. GitHub Actions also
runs these alongside the full Zephyr integration suite. The integration suite
uses real Zephyr 4.4 `west build --cmake-only` configuration for module
discovery, runner metadata, and clean-install coverage, then invokes real
`west flash` and `west debug` commands with `--no-rebuild` in recording mode;
it requires the SDK/toolchain for CMake discovery but does not compile or link
`samples/hello_world`. It uses fixed `rpi_pico` and
`native_sim/native/64` board configurations and does not require hardware:
the configuration-regeneration acceptance uses `west build -d <build> -t help`
as a lightweight test-only mechanism for exercising the same build dependency
and regeneration path without compiling firmware. It then uses recorded
`west flash --no-rebuild` to consume the updated state. User documentation
continues to recommend ordinary `west flash` or `west debug` after the module
is configured into the build; explicit `west build` is also available.

```sh
ZEPHYR_BASE=/path/to/zephyr \
.venv/bin/python -m pytest tests/zephyr_integration/test_adapter.py
```

These compare applicable parser behavior with upstream and execute recording
through the real adapter while rejecting external process, socket, and SSH
operations. The Zephyr compatibility import boundary is enforced by
`scripts/contributor/static_check.py`.

Native SSH tests require an `ssh` executable on `PATH` and a reachable host
from the inventory. Zephyr integration additionally requires a Zephyr checkout,
the `west` executable used by that checkout, and a supported Zephyr
SDK/toolchain. PyYAML and jsonschema must be importable by the Python
environment running pytest. Clean-install checks also require `pyelftools` in
the Python environment used by `west`. Hardware tests require the declared
board, probe, serial endpoint, and remote OpenOCD setup.

GitHub Actions supplies the SSH layer with an ephemeral key and an isolated
container running `sshd`, Python, and OpenOCD. The Zephyr job fetches
Zephyr 4.4.0, updates the modules needed for its test board, and installs the
matching SDK toolchain.
These jobs use `--require-external-tests` so a missing prerequisite or skipped
test fails the job. Physical hardware remains an explicit lab-only validation
layer and never runs on GitHub-hosted runners. Spike and virtual OpenOCD target
testing are not part of this CI setup.

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
.venv/bin/python -m pytest --require-external-tests \
  tests/ssh_integration -m ssh \
  --hardware-inventory /path/to/hardware.yaml

ZEPHYR_BASE=/path/to/zephyr \
WEST=/path/to/west \
.venv/bin/python -m pytest --require-external-tests \
  tests/zephyr_integration -m zephyr

.venv/bin/python -m pytest --require-external-tests \
  tests/hardware -m hardware \
  --hardware-inventory /path/to/hardware.yaml
```

Select one real debug profile without running other destructive nodes by using
its complete parametrized node ID:

```sh
.venv/bin/python -m pytest --require-external-tests \
  'tests/hardware/test_real_debug.py::TestRealOpenOcdDebug::test_debug[board:debug]' \
  --hardware-inventory /path/to/hardware.yaml
```

Run all configured static checks with:

```sh
.venv/bin/python scripts/contributor/static_check.py
```

## Complexity review

Radon reports support refactoring review; they are advisory and are not a CI
threshold. Measure production code normally and test code with assertions
excluded from the control-flow count:

```sh
.venv/bin/python -m radon cc -s -n C python runners scripts
.venv/bin/python -m radon cc -s -n C --no-assert tests
.venv/bin/python -m radon mi -s python runners scripts tests
.venv/bin/python -m radon hal python runners scripts tests
```

Review C-ranked callables and normally refactor D-F production or test-support
callables. A coherent C-ranked test may remain. Any extracted helper must
represent a meaningful domain operation or improve cohesion, reuse, ownership,
testability, or diagnostics; do not add pass-through helpers only to improve a
score. Compare aggregate and maximum complexity with maintainability and
Halstead effort before accepting a change.

## Coverage

The self-contained suite can collect branch coverage for the production
package, west runner entry point, maintained scripts, and locally launched
Python helper subprocesses with:

```sh
.venv/bin/python -m pytest --cov --cov-config=.coveragerc \
  --cov-report=term-missing
```

The self-contained, SSH, and Zephyr jobs collect raw coverage from Python code
running on their host runners. A final reporting job combines the three
coverage data files and writes the aggregate report to its summary. Coverage is
informational; no percentage threshold is enforced. Code run inside the SSH
container and physical hardware is outside this host-side measurement.
