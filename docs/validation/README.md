# Release validation

This directory holds procedures and retained evidence for full release
validation. Developer test selection belongs in
[`docs/development/testing.md`](../development/testing.md); the maintained
requirement-to-test mapping belongs in
[`docs/traceability/acceptance.md`](../traceability/acceptance.md). A passing
test in either location is not, by itself, a retained release-validation
record.

No release validation record exists yet.

## Prerequisites

Run the procedure from the repository root on native Linux. First prepare the
developer environment and understand the external-test safety rules in the
[testing guide](../development/testing.md). The strict driver requires:

- a Zephyr 4.4 source tree and its executable `west`;
- a board name supported by the Zephyr integration tests;
- an ignored hardware inventory whose selected fixtures collectively advertise
  flash, debug, attach, debugserver, thread-info, RTT, and semihosting;
- a completed Zephyr build for the startup benchmark;
- the product YAML configuration and working directory used by that benchmark.

The inventory format and capability rules are documented in
[`hardware_fixtures.md`](../development/hardware_fixtures.md). Inspect the
selected pytest nodes with `--collect-only` before starting. The driver runs
the unit/local, static, Zephyr, native SSH, hardware, and benchmark steps
serially and stops at the first failure. These operations can build, flash,
halt, reset, and resume configured targets.

## Run the validation

Use the Python environment containing the repository's developer dependencies:

```sh
.venv/bin/python scripts/release_validate.py \
  --hardware-config /path/to/hardware.toml \
  --zephyr-base /path/to/zephyr \
  --west /path/to/west \
  --board stm32f746g_disco \
  --benchmark-build-dir /path/to/build \
  --benchmark-config /path/to/remote_openocd.yaml \
  --benchmark-cwd /path/to/zephyr-workspace
```

The driver writes a JSON summary to stdout. It records environment metadata,
advertised fixture capabilities, step return codes, benchmark results, deferred
gates, and local and remote process-leak scans. On a failed step, its captured
output is also written to stderr. PG-012 and PG-013 remain reported as deferred
until they are run in an actual WSL 2 environment; the native-Linux driver does
not turn that deferral into WSL evidence.

## Retain evidence

When a result must be retained, add a dated document in this directory. Record
the exact command, relevant environment and tool versions, selected fixture
profiles, pass/fail result, benchmark result, cleanup result, and deferred
criteria. Do not include credentials, SSH host identities, probe serials,
device paths, populated inventories, or other lab-specific values.
