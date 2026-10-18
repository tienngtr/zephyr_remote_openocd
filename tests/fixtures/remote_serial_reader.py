# SPDX-License-Identifier: Apache-2.0

"""Remote serial observation program, executed as ``python3 -c`` source.

External tests send this file's text to the remote host, so it must remain a
standalone standard-library program. It reports JSON-line events on stdout:
``READY`` after configuring the device, ``ARMED`` after the controller arms it,
then ``MATCH``, ``TIMEOUT``, or ``ERROR``.
"""

import base64
import contextlib
import json
import os
import re
import select
import sys
import termios
import time

MATCH_EXIT_CODE = 0
TIMEOUT_EXIT_CODE = 2
ERROR_EXIT_CODE = 3


def emit(kind: str, **fields: object) -> None:
    print(json.dumps({"type": kind, **fields}, separators=(",", ":")), flush=True)


def configure(fd: int, baud: int, data_bits: int, parity: str, stop_bits: int, flow: str) -> None:
    attrs = termios.tcgetattr(fd)
    speed = getattr(termios, "B" + str(baud), None)
    if speed is None:
        raise ValueError("unsupported baud rate " + str(baud))
    attrs[0] = 0
    attrs[1] = 0
    attrs[3] = 0
    attrs[2] = (
        attrs[2]
        & ~(
            termios.PARENB
            | termios.PARODD
            | termios.CSTOPB
            | termios.CSIZE
            | getattr(termios, "CRTSCTS", 0)
        )
        | termios.CLOCAL
        | termios.CREAD
    )
    attrs[2] |= {5: termios.CS5, 6: termios.CS6, 7: termios.CS7, 8: termios.CS8}[data_bits]
    if parity == "even":
        attrs[2] |= termios.PARENB
    elif parity == "odd":
        attrs[2] |= termios.PARENB | termios.PARODD
    if stop_bits == 2:
        attrs[2] |= termios.CSTOPB
    if flow == "hardware":
        attrs[2] |= getattr(termios, "CRTSCTS", 0)
    if flow == "software":
        attrs[0] |= termios.IXON | termios.IXOFF
    attrs[4] = speed
    attrs[5] = speed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)


def main() -> None:
    device, baud_text, data_bits_text, parity, stop_bits_text, flow, pattern_text, timeout_text = (
        sys.argv[1:]
    )
    baud = int(baud_text)
    data_bits = int(data_bits_text)
    stop_bits = int(stop_bits_text)
    timeout = float(timeout_text)
    fd = None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        configure(fd, baud, data_bits, parity, stop_bits, flow)
        emit("READY")
        while True:
            ready, _, _ = select.select([fd, sys.stdin.buffer], [], [])
            if fd in ready:
                with contextlib.suppress(BlockingIOError):
                    os.read(fd, 65536)
            if sys.stdin.buffer in ready:
                if sys.stdin.buffer.readline().strip() != b"ARM":
                    raise RuntimeError("reader was not armed")
                termios.tcflush(fd, termios.TCIFLUSH)
                break
        deadline = time.monotonic() + timeout
        data = bytearray()
        pattern = re.compile(pattern_text)
        emit("ARMED")
        while True:
            remaining = max(0, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], min(0.2, remaining))
            if fd in ready:
                with contextlib.suppress(BlockingIOError):
                    data.extend(os.read(fd, 65536))
                if pattern.search(data.decode("utf-8", "replace")):
                    emit("MATCH", data=base64.b64encode(data).decode("ascii"))
                    sys.exit(MATCH_EXIT_CODE)
            if time.monotonic() >= deadline:
                break
        emit("TIMEOUT", data=base64.b64encode(data).decode("ascii"))
        sys.exit(TIMEOUT_EXIT_CODE)
    except Exception as exc:
        emit("ERROR", message=str(exc))
        sys.exit(ERROR_EXIT_CODE)
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == "__main__":
    main()
