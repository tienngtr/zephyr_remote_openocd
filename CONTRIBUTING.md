# Contributing

Use Python 3.12 or newer and keep the repository as a self-contained Zephyr
module. Product use does not require pip; the Zephyr-configured Python
environment provides `pyelftools`, PyYAML, and jsonschema. Module developers may
install the convenience tooling in `requirements_dev.txt`; normal users should
not install that file.

Set up the contributor environment and run the ordinary checks with:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements_dev.txt
.venv/bin/python -m pytest
.venv/bin/python scripts/static_check.py
```

The repository map is small: `python/zephyr_remote_openocd/remote/` contains
generic transport and session logic, `zephyr44/runner.py` contains Zephyr 4.4
integration, `runners/remote_openocd.py` registers the west runner,
`remote_helper.py` is the deployed helper, and `resources/` contains the user
configuration contract. Matching tests live under `tests/unit/`,
`tests/local_integration/`, `tests/zephyr_integration/`,
`tests/ssh_integration/`, and `tests/hardware/`.

Production code belongs under `python/zephyr_remote_openocd/` and must remain
board/vendor-agnostic. Keep Zephyr 4.4-specific coupling in `zephyr44/runner.py`,
use only the supported runner interface, and do not access private
`OpenOcdBinaryRunner` members. Do not change the configuration schema or
Protocol v1 unless the work explicitly requires it. When either changes, update
its specification, implementation, examples, and tests together. Version
changes are compatibility decisions, not development-history labels.

Environment variables read by the product use the
`ZEPHYR_REMOTE_OPENOCD_` prefix. Reserve the shorter `ZRO_` prefix for the test
harness, validation tools, test payloads, and generated internal markers. In
particular, `ZEPHYR_REMOTE_OPENOCD_CONFIG` selects the product YAML file, while
`ZRO_HARDWARE_CONFIG` selects the ignored TOML inventory used by external
tests. `ZRO_RECORD` and `ZRO_RECORD_VERSION` control the runner's test-only
[recording mode](docs/development/testing.md#runner-recording-mode). Do not
introduce a public product setting with the `ZRO_` prefix.

Install gitlint's default `commit-msg` hook with:

```sh
.venv/bin/gitlint install-hook
```

The installer refuses to replace an existing `commit-msg` hook. Commit messages
must pass gitlint's default rules; use an imperative subject, a blank line, and
a concise body that explains the change.

Zephyr, SSH, and hardware tests require external environments and ignored
fixture data; see [`docs/development/testing.md`](docs/development/testing.md)
before running them. Update [`SRS`](docs/requirements/SRS.md) for behavior and
requirements, [`SAD`](docs/architecture/SAD.md) for architecture and rationale,
and [`protocol.md`](docs/architecture/protocol.md) for the helper wire contract.
Never commit credentials, host/device details, generated builds, or `.scratch/`
files.

Submit focused commits. Describe behavior, validation performed,
fixture coverage, and any pending WSL validation in pull requests. Update the SRS
or SAD when a lifecycle, protocol, compatibility, or other architectural
decision changes.
