# Contributing

Use Python 3.12 or newer and keep the repository as a self-contained Zephyr
module. Product use does not require pip; the Zephyr-configured Python
environment should provide `pyelftools`. Developer-only tools may be installed
from the repository's development dependency list once it is added.

Production code belongs under `python/zephyr_remote_openocd/` and must remain
board/vendor-agnostic. Keep Zephyr 4.4-specific coupling in `zephyr44/runner.py`,
use only the supported runner surface, and do not access private
`OpenOcdBinaryRunner` members. Preserve the frozen configuration schema and
Protocol 1 contract.

The current hardware-free checks are:

```sh
PYTHONPATH=python python3 -m unittest discover -s tests/unit -v
ruff check .
ruff format --check .
git diff --check
```

Zephyr, SSH, and hardware tests require external environments and ignored
fixture data; see [`tests/README.md`](tests/README.md) before running them.
Never commit credentials, host/device details, generated builds, or `.scratch/`
files. Recording mode must remain free of SSH, OpenOCD, GDB, and hardware I/O.

Submit focused imperative commits. Describe behavior, validation performed,
fixture coverage, and any deferred WSL checks in pull requests. Update the SRS
or SAD when a lifecycle, protocol, compatibility, or other architectural
decision changes.
