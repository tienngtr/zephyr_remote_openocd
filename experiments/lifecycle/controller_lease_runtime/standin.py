# SPDX-License-Identifier: Apache-2.0
"""Harmless real process, driven through inherited command/receipt pipes."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commands', type=int)
    parser.add_argument('--receipts', type=int)
    parser.add_argument(
        '--profile',
        choices=('controlled', 'bind', 'flash-ok', 'flash-fail', 'ready', 'stall'),
        default='controlled',
    )
    args = parser.parse_args()
    descendants: list[int] = []

    def receipt(kind: str, **values: object) -> None:
        if args.receipts is not None:
            os.write(args.receipts, (json.dumps({'kind': kind, **values}) + '\n').encode())

    def ignore(signum: int, _frame: object) -> None:
        receipt('signal', signum=signum)

    def terminate(_signum: int, _frame: object) -> None:
        for pid in descendants:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    receipt('spawned', pid=os.getpid(), generation=int(os.environ['ZRO_LEASE_GENERATION']))
    if args.profile == 'bind' and os.environ['ZRO_LEASE_GENERATION'] == '1':
        os.write(2, b'bind: address already in use\n')
        raise SystemExit(12)
    if args.profile.startswith('flash-'):
        os.write(1, b'flash output\n')
        raise SystemExit(0 if args.profile == 'flash-ok' else 7)
    if args.commands is None:
        if args.profile == 'ready':
            os.write(1, b'INIT\nSTARTUP\n')
        while True:
            signal.pause()
    with os.fdopen(args.commands, 'r', encoding='utf-8') as commands:
        for line in commands:
            command = json.loads(line)
            kind = command['kind']
            if kind == 'write':
                descriptor = 1 if command['stream'] == 'stdout' else 2
                payload = bytes.fromhex(command['hex'])
                view = memoryview(payload)
                while view:
                    view = view[os.write(descriptor, view) :]
            elif kind == 'close-output':
                os.close(1)
                os.close(2)
            elif kind == 'ignore-term':
                signal.signal(signal.SIGTERM, ignore)
            elif kind == 'fork':
                child = os.fork()
                if child == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    os.close(args.commands)
                    receipt('descendant', pid=os.getpid())
                    while True:
                        signal.pause()
                descendants.append(child)
            elif kind == 'exit':
                receipt('exiting', returncode=command['returncode'])
                raise SystemExit(command['returncode'])
            else:
                raise ValueError('unknown stand-in command')
            receipt('ack', command=kind)


if __name__ == '__main__':
    main()
