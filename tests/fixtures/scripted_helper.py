# SPDX-License-Identifier: Apache-2.0

"""A helper control peer that replays a fixed event script, then exits.

Arguments are ``EXIT_CODE EVENTS START``. The peer writes SESSION_CREATED,
reads one controller line, which must be START when ``START`` is ``start``
rather than ``any``, writes the ``EVENTS`` text verbatim, and exits with
``EXIT_CODE``.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    exit_code, events, start = sys.argv[1:4]
    created = {
        "version": 1,
        "type": "SESSION_CREATED",
        "helper": "test",
        "session_id": "id",
        "remote_workspace": "/workspace",
    }
    print(json.dumps(created, separators=(",", ":")), flush=True)
    line = sys.stdin.buffer.readline()
    if start == "start":
        assert json.loads(line)["type"] == "START"
    else:
        assert start == "any"
    sys.stdout.write(events)
    sys.stdout.flush()
    sys.exit(int(exit_code))


if __name__ == "__main__":
    main()
