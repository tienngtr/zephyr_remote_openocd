# SPDX-License-Identifier: Apache-2.0

"""A synthetic OpenOCD whose escaped descendant keeps writing its log.

Arguments: the log channel as the helper materialized it (one Tcl word) and a
PID file. The descendant leaves the process group, detaches from the child's
stdout and stderr, and writes the log channel until the channel closes. The
leader records the descendant's PID, announces ``written``, and waits.
"""

from __future__ import annotations

import os
import re
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.readiness_evidence import announce


def main() -> None:
    word, pid_file = sys.argv[1], Path(sys.argv[2])
    # Undo the helper's double-quoted Tcl word, as OpenOCD's Tcl would.
    path = re.sub(r"\\(.)", r"\1", word[1:-1])
    reader, writer = os.pipe()
    descendant = os.fork()
    if descendant == 0:
        os.close(reader)
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for stream in (0, 1, 2):
            os.dup2(devnull, stream)
        with open(path, "wb", buffering=0) as log:
            os.write(writer, b"ready")
            os.close(writer)
            block = b"x" * (64 * 1024)
            try:
                while True:
                    log.write(block)
            except BrokenPipeError:
                os._exit(0)
    os.close(writer)
    assert os.read(reader, len(b"ready")) == b"ready"
    os.close(reader)
    pid_file.write_text(str(descendant))
    announce("written")
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
