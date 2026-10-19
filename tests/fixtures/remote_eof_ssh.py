# SPDX-License-Identifier: Apache-2.0

"""A non-conforming SSH stand-in that can end only the remote controller input.

Arguments are ``ROOT host command``, as for ``local_ssh.py``. Finite helper
commands run directly in the disposable account under ``ROOT``. The control
command is relayed through pipes. One byte read from the FIFO ``ROOT/inject``
closes the helper's stdin while local stdin stays open, discards later local
input, and withholds helper stdout until local stdin reaches EOF; the relay
then acknowledges ``E`` on the FIFO ``ROOT/injected``. Local stdin EOF before
injection is ordinary directional EOF.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO


class Relay:
    def __init__(self, helper: subprocess.Popen[bytes]) -> None:
        assert helper.stdin is not None
        self.helper = helper
        self.helper_input: IO[bytes] | None = helper.stdin
        self.lock = threading.Lock()
        self.injected = False
        self.local_closed = threading.Event()

    def close_helper_input(self) -> None:
        with self.lock:
            if self.helper_input is not None:
                self.helper_input.close()
                self.helper_input = None

    def inject(self, root: Path) -> None:
        with (root / "inject").open("rb", buffering=0) as inject:
            if not inject.read(1):
                return
        with self.lock:
            self.injected = True
        self.close_helper_input()
        with (root / "injected").open("wb", buffering=0) as injected:
            injected.write(b"E")

    def forward_input(self) -> None:
        while chunk := os.read(0, 65536):
            with self.lock:
                if self.helper_input is not None:
                    try:
                        self.helper_input.write(chunk)
                        self.helper_input.flush()
                    except BrokenPipeError:
                        self.helper_input = None
        self.local_closed.set()
        self.close_helper_input()

    def forward_output(self) -> None:
        assert self.helper.stdout is not None
        held = bytearray()
        while True:
            chunk = os.read(self.helper.stdout.fileno(), 65536)
            with self.lock:
                hold = self.injected and not self.local_closed.is_set()
            if hold:
                held.extend(chunk)
                if chunk:
                    continue
                self.local_closed.wait()
            if held:
                os.write(1, bytes(held))
                held.clear()
            if not chunk:
                return
            os.write(1, chunk)


def main() -> None:
    root = Path(sys.argv[1])
    assert sys.argv[2] == "test-host"
    argv = shlex.split(sys.argv[3])
    assert argv[0] == "python3"
    argv[0] = sys.executable
    environment = os.environ.copy()
    environment["HOME"] = str(root / "home")
    environment["XDG_RUNTIME_DIR"] = str(root / "runtime")
    if argv[-1] != "control":
        os.execve(sys.executable, argv, environment)
    helper = subprocess.Popen(argv, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    relay = Relay(helper)
    threading.Thread(target=relay.inject, args=(root,), daemon=True).start()
    threading.Thread(target=relay.forward_input, daemon=True).start()
    relay.forward_output()
    sys.exit(helper.wait())


if __name__ == "__main__":
    main()
