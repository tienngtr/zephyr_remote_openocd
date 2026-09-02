# Contributing

Use Python 3.12 or newer and keep the repository as a self-contained Zephyr
module. Product use does not require pip; the Zephyr-configured Python
environment should provide `pyelftools`. Developer-only tools may be installed
from `requirements-dev.txt`; they are not needed by product users.

Production code belongs under `python/zephyr_remote_openocd/` and must remain
board/vendor-agnostic. Keep Zephyr 4.4-specific coupling in `zephyr44/runner.py`,
use only the supported runner surface, and do not access private
`OpenOcdBinaryRunner` members. Preserve the frozen configuration schema and
Protocol 1 contract.

The current hardware-free checks are:

```sh
pytest
ruff check .
ruff format --check .
git diff --check
```

Zephyr, SSH, and hardware tests require external environments and ignored
fixture data; see [`docs/development/testing.md`](docs/development/testing.md)
before running them.
Never commit credentials, host/device details, generated builds, or `.scratch/`
files. Recording mode must remain free of SSH, OpenOCD, GDB, and hardware I/O.

Submit focused imperative commits. Describe behavior, validation performed,
fixture coverage, and any deferred WSL checks in pull requests. Update the SRS
or SAD when a lifecycle, protocol, compatibility, or other architectural
decision changes.
