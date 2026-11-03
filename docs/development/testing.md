# Testing layers

The contributor-facing validation policy, including which layers each kind of
change requires, is the
[validation checklist](../../CONTRIBUTING.md#validation-checklist). This page
provides the layer-specific commands and external-test setup details.

Choose the smallest layer that covers the change:

| Layer | Directory | Requires |
| --- | --- | --- |
| Self-contained | Default selection in `pytest.ini` | The [test environment](#set-up-the-test-environment); `cmake`, Ninja, and `make` on `PATH`; an OpenSSH client for the static checks; optionally a host `gdb` |
| Zephyr integration | `tests/zephyr_integration/` | A Zephyr 4.4.x or 4.5.x source tree and SDK; see [Zephyr integration tests](#zephyr-integration-tests) |
| SSH integration | `tests/ssh_integration/` | A local hardware inventory and a reachable SSH host; see [SSH integration tests](#ssh-integration-tests) |
| Hardware | `tests/hardware/` | A local hardware inventory, the local Zephyr build environment and GDB it declares, a board, probe, serial endpoint, and remote OpenOCD; see [Hardware tests](#hardware-tests) |

A plain `.venv/bin/python -m pytest` run never connects to an SSH host and
needs no Zephyr checkout or lab hardware. Each external layer runs only when
its directory is named on the command line.

## Set up the test environment

Create the repository's test-only Python environment and install its developer
tools. This environment runs both self-contained and external pytest layers;
it does not install or package the production module:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements_dev.txt
```

Normal product use instead runs through Zephyr's configured Python environment.
Running pytest from Zephyr's environment is also valid if that interpreter has
every dependency in `requirements_dev.txt`.

Tests clear product configuration and recording environment variables and set
their own values, independently of the developer's shell configuration.

## Self-contained suite

Run the checks that every change needs:

```sh
.venv/bin/python -m pytest
.venv/bin/python scripts/contributor/static_check.py
```

The ordinary pytest command runs only hardware-free unit and local-process
tests:

- The local interactive Ctrl-C ownership tests drive a host `gdb` on a
  pseudo-terminal and are skipped when no `gdb` executable is on `PATH`.
- Tcl escaping tests need no Tcl interpreter.
- The local CMake configuration tests use the `cmake` on `PATH` with Ninja and
  Unix Makefiles, and fail rather than skip when those tools are missing.

When changing the module's CMake integration, also run the CMake configuration
tests with CMake 3.20.5, the minimum that Zephyr 4.4 supports, because
configuration change detection depends on CMake generator behavior. GitHub
Actions runs the same check on Python 3.12:

```sh
python3 -m venv .scratch/cmake-3.20.5
.scratch/cmake-3.20.5/bin/python -m pip install cmake==3.20.5
PATH="$(.scratch/cmake-3.20.5/bin/python -c 'import cmake; print(cmake.CMAKE_BIN_DIR)'):$PATH" \
.venv/bin/python -m pytest tests/local_integration/test_cmake_configuration.py
```

## External tests

> [!WARNING]
> SSH and hardware tests can change external state, and hardware tests flash
> targets. Confirm the selection with `--collect-only` before running external
> nodes, keep destructive profiles serial, and inspect cleanup output before
> reusing a target.

### Prepare a hardware inventory

SSH integration and hardware tests read a contributor-provided YAML hardware
inventory, selected with the pytest option `--hardware-inventory`. Create one
by following the
[hardware inventory quick start](hardware_inventories.md#quick-start), which
copies the starter to `.scratch/config/hardware.yaml`, validates it, and
explains when to omit `--check-local` for an SSH-only inventory. The SSH cases
do not execute target build fields.

The commands below use `.scratch/config/hardware.yaml`; substitute your
inventory path if it differs.

### Collect and run

SSH integration and hardware tests share this flow. Zephyr integration tests
need no inventory; see [Zephyr integration tests](#zephyr-integration-tests).

Collect before execution. Name the external test directories; without them,
pytest collects only the default unit and local integration suites configured
in `pytest.ini`:

```sh
.venv/bin/python -m pytest --collect-only -q \
  tests/ssh_integration tests/hardware \
  --hardware-inventory .scratch/config/hardware.yaml
```

Then run the desired layers explicitly:

```sh
.venv/bin/python -m pytest --require-external-tests \
  tests/ssh_integration -m ssh \
  --hardware-inventory .scratch/config/hardware.yaml

.venv/bin/python -m pytest --require-external-tests \
  tests/hardware -m hardware \
  --hardware-inventory .scratch/config/hardware.yaml
```

`--require-external-tests` fails the run if any test is skipped, so that a
missing prerequisite cannot pass unnoticed. It also fails on skips that are
expected for a given setup, such as:

- hardware nodes for capabilities that the inventory declares no profile for;
  `tests/fixtures/hardware.example.yaml` declares only `flash`;
- real-OpenOCD SSH nodes when the remote OpenOCD lacks the `dummy` adapter or
  is not executable.

To keep those expected skips from failing the run, select only nodes that the
inventory and remote host support, using node IDs or `-k` filters. For example,
to run one debug profile without other destructive nodes:

```sh
.venv/bin/python -m pytest --require-external-tests \
  'tests/hardware/test_real_debug.py::TestRealOpenOcdDebug::test_debug[board:debug]' \
  --hardware-inventory .scratch/config/hardware.yaml
```

Replace `board:debug` with `<target>:<profile>` from your inventory, or copy
the exact ID from the `--collect-only` output.

### SSH integration tests

SSH integration tests use the first host declared in the inventory and its
configured SSH command, which defaults to `ssh` on `PATH`. The host must be
reachable and provide Python 3.12 or newer. The real-OpenOCD nodes for
readiness and the log relay also need a remote OpenOCD build with the `dummy`
adapter and are skipped without one.

### Zephyr integration tests

Zephyr integration requires a Zephyr checkout and a supported Zephyr
SDK/toolchain. PyYAML and jsonschema must be importable by the Python
environment running pytest. Each run uses the Zephyr tree selected by
`ZEPHYR_BASE`; covering both supported release families takes a run against
each.

Which `west` the tests use depends on the module:

- `test_zephyr_integration.py` uses `WEST`, or `west` on `PATH` when `WEST` is
  unset. Its clean-install checks also require `pyelftools` in the Python
  environment used by that `west`.
- The adapter, selection, and source-switching tests import or run `west` and
  Zephyr's scripts through the interpreter running pytest. Install Zephyr's
  script requirements into that environment, as CI does, so that those imports
  succeed:

  ```sh
  .venv/bin/python -m pip install -r /path/to/zephyr/scripts/requirements-base.txt
  ```

Run the main suite once per supported release family. Replace
`/path/to/zephyr` with the Zephyr checkout and `/path/to/west` with its `west`
executable. The source-switching tests have their own prerequisites and run
separately, as described in [Source-switching tests](#source-switching-tests):

```sh
ZEPHYR_BASE=/path/to/zephyr \
WEST=/path/to/west \
.venv/bin/python -m pytest --require-external-tests \
  tests/zephyr_integration -m zephyr \
  --ignore=tests/zephyr_integration/test_switching.py
```

The main suite uses real Zephyr 4.4.x/4.5.x `west build --cmake-only`
configuration for module discovery, runner metadata, and clean-install
coverage, then invokes real `west flash` and `west debug` commands with
`--no-rebuild` in recording mode. It requires the SDK/toolchain for CMake
discovery but does not compile or link `samples/hello_world`. It uses fixed
`rpi_pico` and `native_sim/native/64` board configurations and does not require
hardware. The configuration-regeneration acceptance uses
`west build -d <build> -t help` as a lightweight test-only mechanism for
exercising the same build dependency and regeneration path without compiling
firmware, then uses recorded `west flash --no-rebuild` to consume the updated
state.

To check only the adapter contract, without an SDK, board, or firmware build:

```sh
ZEPHYR_BASE=/path/to/zephyr \
.venv/bin/python -m pytest tests/zephyr_integration/test_adapter.py
```

These tests compare applicable parser behavior with upstream and execute
recording through the real runner integration while rejecting external
process, socket, and SSH operations. They use no SSH server, OpenOCD, or
hardware. A local-process concurrency test also exercises real deployment,
staging, and session cleanup through a configured local SSH replacement,
synchronized by FIFO gates and an explicit post-open event rather than timing;
the adapter's remaining concurrency policy is reviewed design evidence. The CI
Zephyr suites include these tests.

#### Type-checking against a Zephyr checkout

Ordinary static checks see Zephyr's `runners` package as untyped, so the
adapter's upstream base class and runner attributes are unchecked there. The
Zephyr integration CI job also type-checks the repository against the selected
checkout's runners. To run the same check locally, replace `/path/to/zephyr`
with the checkout, and replace `zephyr44` with `zephyr45` when that checkout is
Zephyr 4.5.x; the inactive adapter must not be checked against the other
release's incompatible constructor:

```sh
MYPYPATH=/path/to/zephyr/scripts/west_commands \
.venv/bin/python -m mypy --config-file=mypy.ini --follow-imports=silent \
  python/zephyr_remote_openocd/_runner.py python/zephyr_remote_openocd/_zephyr.py \
  python/zephyr_remote_openocd/zephyr44 runners/remote_openocd.py tests/zephyr_integration
```

Ordinary static checks still cover all repository Python files, and
`scripts/contributor/static_check.py` enforces the Zephyr compatibility import
boundary.

#### Source-switching tests

The source-switching tests require a local Git repository containing both
`v4.4.0` and `v4.5.0` tags, a matching SDK, and modules that configure under
both releases, such as the Zephyr 4.4.0 manifest's CMSIS and Pico modules. The
4.5-pinned CMSIS module cannot configure the 4.4 Cortex-M fixture because it
removed the module-level `CMakeLists.txt` that fixture requires; when the
sibling `modules` directory holds 4.5-pinned modules, set `ZRO_SWITCH_MODULES`
as described below. These configuration fixtures do not promise arbitrary module-version
interchangeability or cross-release firmware-build compatibility.

The tests create disposable clones and build directories under `.scratch/`,
preserving the contributor's checkout. Collect first, then run:

```sh
ZEPHYR_BASE=/path/to/zephyr \
ZRO_SWITCH_ZEPHYR_REPOSITORY=/path/to/zephyr \
.venv/bin/python -m pytest tests/zephyr_integration/test_switching.py --collect-only

ZEPHYR_BASE=/path/to/zephyr \
ZRO_SWITCH_ZEPHYR_REPOSITORY=/path/to/zephyr \
.venv/bin/python -m pytest --require-external-tests \
  tests/zephyr_integration/test_switching.py
```

The tests skip when a required tag or the `west` package is unavailable, so
`--require-external-tests` makes such a skip fail the run.

`ZRO_SWITCH_ZEPHYR_REPOSITORY` defaults to `ZEPHYR_BASE`. Modules default to
the repository's sibling `modules` directory; when they are elsewhere, set
`ZRO_SWITCH_MODULES` to a semicolon-separated list for CMake's
`ZEPHYR_MODULES`, independently of runner activation. No adapter override is
provided.

The tests use real regeneration via `west build -t help`, then recording with
current metadata and explicit no-build artifacts; they do not compile or
execute firmware. A stock-runner control checks deliberately stale Pico 2
arguments. Fresh subprocess probes verify adapter identity and one concrete
registration before regeneration.

### Hardware tests

Hardware tests require the declared board, probe, serial endpoint, and remote
OpenOCD setup.

#### Build cache

Hardware preparation reuses incremental Zephyr build directories between pytest
runs, under `.scratch/hardware/build-cache-<build-input-hash>/`. The harness
builds with `--pristine=never`, so a cached build directory is never cleaned
automatically. To force a fresh build, remove its `build-cache-*` directory;
Zephyr and ccache data in the shared cache below are kept.

The hash covers the repository path, the inventory's build environments
(including the baseline host environment passed to west), each target's host
reference, and the build fields; it excludes host transport, serial, and
operation settings. Changing any covered input selects a new cache directory
and a cold build.

Zephyr's `USER_CACHE_DIR` and ccache use a separate
`shared-cache-<environment-hash>/` namespace keyed by the repository path and
Zephyr build environment, so compatible inventories share compiler and Zephyr
caches. An explicit `-DUSER_CACHE_DIR=...` in a build's `cmake_args` is used
instead of the shared Zephyr cache.

Cold builds pass the inventory's CMake arguments and redirected Zephyr cache to
west. Warm builds omit the CMake argument section so west can run Ninja
directly without an otherwise redundant CMake configure step. Generated product
configurations are only rewritten when their contents change, preserving
Ninja's no-op path.

#### Timing a hardware run

To identify whether a slow hardware run is spending time preparing firmware or
performing the board operation, add `--hardware-timings`. It reports each
recipe's build duration and cache state (`cold`, `warm`, `session`, or
`failed`) and
each hardware node's pytest setup, call, and teardown durations:

```sh
.venv/bin/python -m pytest --hardware-timings \
  tests/hardware -m hardware \
  --hardware-inventory .scratch/config/hardware.yaml
```

The timing option does not change test selection or execution.

#### What hardware acceptance tests verify

**Debug.** Real `west debug` acceptance is source-level and
architecture-independent. The selected debug profile supplies an ELF, a
breakpoint symbol, and a distinct precondition build. The test flashes the
precondition, then GDB loads the selected ELF. Before continuing, GDB reads a
memory witness derived from the two ELFs, which must hold the selected image's
bytes. GDB then continues without a post-load reset, stops at the symbol, reads
`$pc` and the current instruction, and detaches. Without the load, the witness
still holds the precondition bytes and the test fails. This deliberately does
not use serial output: RAM-loaded targets can lose the new image on reset, and
Zephyr does not promise fresh application output from `west debug`.

**Flash.** Real `west flash` acceptance uses two distinct images. It first
flashes a quiet precondition image (normally `samples/basic/minimal`), then
arms serial observation before flashing the selected image (normally
`samples/hello_world`) and requires its marker. Precondition quietness is a
test invariant; the test does not spend a timed observation re-proving it.

**Attach.** Attach acceptance reads the PC, the current instruction, and the
same kind of memory witness without loading, and requires the precondition
bytes.

**Debugserver.** Debugserver acceptance likewise flashes a quiet precondition,
then arms serial observation before an independent GDB client loads and starts
the selected image through debugserver, and requires its marker.

**RTT server.** RTT-server acceptance combines an active GDB session with a
bidirectional RTT exchange. The debugserver variant waits for a connection to
the local GDB endpoint before launching its independent client; runner
diagnostic wording is not a readiness interface.

**`--log-file`.** On Zephyr 4.4 builds, the `--log-file` nodes require the
runner to reject the option before any effect, including on the local file. On
Zephyr 4.5 builds, the stalled-consumer nodes stop the local `west` process
with `SIGSTOP` while its SSH transports keep running, and flood OpenOCD's log
beyond the remote helper's relay buffer. The flash node stops `west` once the
remote OpenOCD process is observed and requires fresh serial output from the
selected image while `west` is stopped; the debugserver node stops it once the
local Tcl endpoint accepts connections, then requires Tcl and an independent
GDB client to keep working. Both require an incomplete-log failure and a local
file whose flood records form a gapless prefix. The semihosting node requires
the console output on the runner's standard output and not in the log file.

## Runner recording mode

Set `ZRO_RECORD=1` to make `remote_openocd` construct the requested operation
and print its structured JSON record instead of executing it. Replace `lab`
with a remote name from your product configuration:

```sh
ZRO_RECORD=1 west flash -r remote_openocd --remote lab
```

The runner still loads product configuration and local build metadata needed to
construct the plan. It returns before deploying the helper or starting SSH,
OpenOCD, GDB, port forwarding, or hardware access, and it does not create,
open, inspect, or modify a `--log-file` file. Values other than the exact
string `1` do not enable recording.

Recording verifies configuration and command construction only. It does not
exercise packaged-helper discovery or deployment, remote connectivity,
resolution of `~` in remote paths, process lifecycle and cleanup, target
state, or hardware behavior. Do not use a recording result as acceptance
evidence for those behaviors.

Recording a `debug`, `attach`, `debugserver`, or `rtt` operation fails for a
build that enables thread information on an RTOS-eligible target unless
`ZRO_RECORD_OPENOCD_VERSION` is set. Set it to representative output from
`openocd --version`:

```sh
ZRO_RECORD=1 \
ZRO_RECORD_OPENOCD_VERSION='Open On-Chip Debugger 0.12.0' \
west debug -r remote_openocd --remote lab
```

The injected version lets the runner construct its thread-awareness plan
without querying remote OpenOCD. It is ignored when thread information is not
requested or the target is ineligible under the selected adapter. It is not a
substitute for testing the configured remote executable.

## Continuous integration

GitHub Actions runs on Linux hosts. Its independent validation jobs are:

- **Self-contained suite** on Python 3.12, 3.13, 3.14, and 3.15, using parallel
  `pytest-xdist` workers.
- **Static checks.**
- **SSH suite** against an isolated SSH server container with its own
  filesystem, process namespace, and network namespace. The job supplies an
  ephemeral key, and the container runs `sshd`, Python, and OpenOCD; the tests
  use the real SSH client and server.
- **Zephyr suites** against v4.4.0 and v4.5.0 baselines with real workspaces
  and SDKs. They run real Zephyr CMake configuration and west runner commands
  in recording mode; they do not compile or execute firmware. They update the
  modules needed for their test board and install the matching SDK toolchain.
  The release-tag checkouts are CI baselines, not product-version
  restrictions.
- **Source switching** in its own job on a v4.4.0 workspace; the other Zephyr
  jobs exclude it.
- **Rolling Zephyr checks** against the upstream `v4.4-branch` and
  `v4.5-branch` maintenance branches. They detect possibly breaking changes
  from upstream, are non-gating, and run on `main`, nightly, and on manual
  dispatch rather than on every branch push.
- **Coverage report**, described in [Coverage](#coverage).

The SSH and Zephyr jobs use `--require-external-tests`, so a missing
prerequisite or skipped test fails the job.

When jobs run:

- A branch whose changes from `main` touch only `docs/` and Markdown files
  skips the SSH and Zephyr suites and the coverage report; static checks and
  the self-contained suite still run.
- Release tag pushes do not run the workflow.

The source-switching job reuses the 4.4.0 manifest's CMSIS and Pico modules, as
described in [Source-switching tests](#source-switching-tests); the ordinary
4.5 suite retains its own manifest-pinned modules.

Physical hardware remains an explicit lab-only validation layer and never runs
on GitHub-hosted runners. Spike and virtual OpenOCD target testing are not part
of this CI setup.

## Test harness behavior

This section describes harness internals that matter when writing or
debugging tests; running tests needs no action.

- **Inventory data.** The hardware inventory is not a pytest fixture: pytest
  fixtures inject selected inventory data into tests, while the harness turns
  that data into prepared operations and a configured test environment before
  an external test runs.

- **Deadlines.** Test-owned pipe reads have deadlines, including partial-line
  and EOF waits. The developer dependency `pytest-timeout` supplies a generous
  30-minute per-test backstop; operation-specific timeouts remain the primary
  diagnostics.
- **Process groups.** Hardware operation and independent GDB commands run in
  isolated process groups owned by scoped cleanup; firmware preparation builds
  run as ordinary timed subprocesses. Launch and adoption defer the parent's
  SIGINT handler without blocking SIGINT in the child; a deferred interrupt
  rolls back the acquired process before publication. Cleanup interrupts a
  live leader with a bounded wait, kills remaining members even when the
  leader already exited, reaps the leader, joins any owned output reader under
  a deadline, and closes captured pipes. An existing operation failure remains
  primary if cleanup also fails.
- **Transport cleanup.** Serial readers and SSH test processes use the managed
  transport cleanup path, including finite termination/escalation waits and
  independent attempts to close their diagnostic drains and data pipes. Test
  cleanup scopes retain an existing assertion or operation failure and attach
  secondary cleanup diagnostics. Local serial-reader tests use the same
  managed-process ownership as external readers.
- **Shared helpers.** Shared hardware operation and acceptance helpers live
  under `tests/acceptance/`. Hardware pytest nodes and local environment,
  cleanup, and GDB/RTT regressions call those helpers directly; pytest test
  methods are not reusable operation APIs.

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
package, west runner entry point, maintained scripts, the module's CMake-time
scripts, and locally launched Python helper subprocesses with:

```sh
.venv/bin/python -m pytest --cov --cov-config=.coveragerc \
  --cov-report=term-missing
```

In CI, the self-contained (Python 3.12), SSH, Zephyr 4.4.0, Zephyr 4.5.0, and
source-switching jobs collect raw coverage from Python code running on their
host runners; the rolling-branch jobs are not measured. A final reporting job
combines those coverage data files and writes the aggregate report to its
summary. Coverage is informational; no percentage threshold is enforced.

Code run inside the SSH container and on physical hardware is outside this
host-side measurement. Coverage jobs also skip tracing Python processes whose
main script is in a Zephyr tree's `scripts` directory, such as the Kconfig and
devicetree helpers. Those processes never execute measured code, and tracing
them roughly doubles each configuration; `tests/ci/coverage/` implements the
exclusion.
