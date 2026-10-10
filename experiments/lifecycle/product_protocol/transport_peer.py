# SPDX-License-Identifier: Apache-2.0
"""Local experiment subprocess: expose START, EOF, stdout and stderr handshakes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    mode, path = sys.argv[1:]
    receipt = Path(path)
    if mode == 'hold':
        print('MASTER_READY', flush=True)
        sys.stdin.buffer.read()
        print('MASTER_ENDED', flush=True)
        return
    if mode == 'watch':
        print('WATCHING', flush=True)
        with receipt.open(encoding='utf-8') as stream:
            print(stream.read(), end='', flush=True)
        return
    print('SESSION_CREATED', flush=True)
    if sys.stdin.buffer.readline() != b'START\n':
        raise ValueError('expected experimental START')
    print('START_ACCEPTED', flush=True)
    print('diagnostic before EOF', file=sys.stderr, flush=True)
    if sys.stdin.buffer.read():
        raise ValueError('lease requires no further commands')
    # Physical receipt is independent of protocol-output success.
    with receipt.open('w', encoding='utf-8') as stream:
        stream.write('CLEANED\n')
    try:
        print(json.dumps({'type': 'SESSION_ENDED', 'cause': 'controller_ended'}), flush=True)
        print('diagnostic after EOF', file=sys.stderr, flush=True)
    except BrokenPipeError:
        # Lost transport cannot report a final result. Physical receipt already exists.
        with open('/dev/null', 'w', encoding='utf-8') as null:
            os.dup2(null.fileno(), sys.stdout.fileno())


if __name__ == '__main__':
    main()
