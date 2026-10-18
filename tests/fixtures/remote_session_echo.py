# SPDX-License-Identifier: Apache-2.0

"""Remote TCP echo service, executed as ``python3 -c`` source.

SSH integration sends this file's text to the remote host, so it must remain a
standalone standard-library program. Arguments are ``ADDRESS PORT``; it prints
``ZRO_TEST_READY`` once listening. With a third ``STATE`` argument it first
appends startup work to ``STATE``, prints ``ZRO_TEST_STARTUP_WORK``, and holds
a colliding listener so its own bind fails with ``EADDRINUSE``.
"""

import socket
import sys
from pathlib import Path


def main() -> None:
    address, port = sys.argv[1], int(sys.argv[2])
    collision = None
    if len(sys.argv) > 3:
        with Path(sys.argv[3]).open("a") as side_effect:
            side_effect.write("startup work")
        print("ZRO_TEST_STARTUP_WORK", flush=True)
        collision = socket.socket()
        collision.bind((address, port))
        collision.listen()
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((address, port))
    listener.listen()
    print("ZRO_TEST_READY", flush=True)
    while True:
        connection, _ = listener.accept()
        with connection:
            while data := connection.recv(65536):
                connection.sendall(data)


if __name__ == "__main__":
    main()
