# SPDX-License-Identifier: Apache-2.0

"""A child that performs startup work, then fails on an address collision.

Arguments are ``ADDRESS PORT FAILURE``. ``diagnostic`` reports the collision
without binding; any other failure binds a real listener twice so the second
bind raises ``EADDRINUSE``. Startup work is appended to
``$ZRO_COLLISION_STATE`` so tests can detect a repeated attempt.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def main() -> None:
    address, port, failure = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    with Path(os.environ["ZRO_COLLISION_STATE"]).open("a") as side_effect:
        side_effect.write("startup work\n")
    if failure == "diagnostic":
        print("Error: could not bind: Address already in use", file=sys.stderr, flush=True)
        raise SystemExit(7)
    collision = socket.socket()
    collision.bind((address, port))
    collision.listen()
    listener = socket.socket()
    listener.bind((address, port))


if __name__ == "__main__":
    main()
