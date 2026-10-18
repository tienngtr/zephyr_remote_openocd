# SPDX-License-Identifier: Apache-2.0

"""Remote helper installation program, executed as ``python3 -c`` source.

Deployment sends this file's text, not an import of it, so it must remain a
standalone standard-library program. It reads the helper bundle from stdin and
prints one ``DEPLOYED`` JSON response.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time

STALE_HELPER_AGE_SECONDS = 24 * 60 * 60


def main() -> None:
    data = sys.stdin.buffer.read()
    digest = hashlib.sha256(data).hexdigest()
    base = pathlib.Path.home() / ".local/libexec/zephyr_remote_openocd/protocol_v1"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    with (base / ".deploy.lock").open("a+b") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = base / ("helper-" + digest + ".py")
        reused = False
        try:
            reused = target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest
        except OSError:
            reused = False
        if not reused:
            fd, tmp = tempfile.mkstemp(prefix=".helper_", dir=base)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, target)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
        os.chmod(target, 0o600)
        os.utime(target, None)
        cutoff = time.time() - STALE_HELPER_AGE_SECONDS
        for candidate in base.glob("helper-*.py"):
            if candidate == target:
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                pass
    print(
        json.dumps(
            {
                "version": 1,
                "type": "DEPLOYED",
                "status": "reused" if reused else "deployed",
                "path": str(target.resolve()),
                "sha256": digest,
            }
        )
    )


if __name__ == "__main__":
    main()
