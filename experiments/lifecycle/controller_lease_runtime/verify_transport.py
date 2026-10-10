# SPDX-License-Identifier: Apache-2.0
"""Configured production SSH abstraction, real helper, inetd OpenSSH and sharing.

No external hosts, listener, hardware, or checked-in keys. Run explicitly; all
transport configuration and generated keys stay under ignored scratch space.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import selectors
import shlex
import shutil
import sys
from pathlib import Path

from zephyr_remote_openocd.remote.ssh import SshCommand, _stop_process

from experiments.lifecycle.product_protocol.transport import local_command, run, ssh_command

ROOT = Path(__file__).resolve().parent


class Frames:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.buffer = bytearray()

    def read(self) -> dict[str, object]:
        while b'\n' not in self.buffer:
            with selectors.DefaultSelector() as watch:
                watch.register(self.descriptor, selectors.EVENT_READ)
                if not watch.select(30):
                    raise TimeoutError('helper protocol handshake')
            chunk = os.read(self.descriptor, 65536)
            if not chunk:
                raise EOFError('helper exited without required frame')
            self.buffer.extend(chunk)
        line, rest = self.buffer.split(b'\n', 1)
        self.buffer = bytearray(rest)
        result: dict[str, object] = json.loads(line)
        return result

    def until(self, kind: str) -> dict[str, object]:
        while True:
            frame = self.read()
            if frame['type'] == kind:
                return frame


def exercise(command: SshCommand, workspace: Path, *, abort: bool) -> dict[str, object]:
    remote = shlex.join(
        [
            sys.executable,
            '-u',
            '-m',
            'experiments.lifecycle.controller_lease_runtime.helper',
            '--workspace',
            str(workspace),
        ]
    )
    remote = shlex.join(
        ['/bin/sh', '-c', f'cd {shlex.quote(str(ROOT.parents[2]))} && exec {remote}']
    )
    process = command.popen('experiment.invalid', remote)
    helper_pid: int | None = None
    child_pid: int | None = None
    try:
        assert process.stdout is not None and process.stdin is not None
        frames = Frames(process.stdout.fileno())
        created = frames.until('SESSION_CREATED')
        helper_pid = int(str(created['helper_pid']))
        process.stdin.write(
            (
                json.dumps(
                    {
                        'type': 'START',
                        'argv': [
                            sys.executable,
                            '-u',
                            str(ROOT / 'standin.py'),
                            '--profile',
                            'ready',
                        ],
                        'required': ['INIT', 'STARTUP'],
                        'policy': 'live',
                        'max_attempts': 2,
                    }
                )
                + '\n'
            ).encode()
        )
        process.stdin.flush()
        attempt = frames.until('ATTEMPT')
        assert attempt['generation'] == 1 and attempt['argv']
        ready = frames.until('READY')
        child_pid = int(str(ready['pid']))
        descriptor = os.pidfd_open(helper_pid)
        try:
            if abort:
                process.kill()
                process.wait(timeout=30)
            else:
                process.stdin.close()
                ended = frames.until('SESSION_ENDED')
                assert ended['trigger'] == 'controller-ended'
                assert ended['disposal_confirmed'] is True
                assert process.wait(timeout=30) == 0
            with selectors.DefaultSelector() as watch:
                watch.register(descriptor, selectors.EVENT_READ)
                assert watch.select(30), 'helper failed to exit after controller loss'
        finally:
            os.close(descriptor)
        assert not workspace.exists(), 'controller loss left a live workspace'
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError('controller loss orphaned child')
        return {'final_received': not abort, 'cleanup_confirmed_by_observation': True}
    finally:
        _stop_process(process)
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child_pid, 9)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--openssh', action='store_true')
    args = parser.parse_args()
    scratch = Path('.scratch/agents/controller-lease-runtime/transport').resolve()
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, mode=0o700)
    results: dict[str, object] = {}
    command = local_command(scratch)
    for abort in (False, True):
        results[f'pipe-{abort}'] = exercise(command, scratch / f'pipe-{abort}', abort=abort)
    if args.openssh:
        ssh, sshd, keygen = (shutil.which(name) for name in ('ssh', 'sshd', 'ssh-keygen'))
        assert ssh and sshd and keygen
        command = ssh_command(scratch, ssh, sshd, keygen)
        results['openssh_version'] = run([ssh, '-V']).stderr.decode().strip()
        for abort in (False, True):
            results[f'ssh-{abort}'] = exercise(command, scratch / f'ssh-{abort}', abort=abort)
        sharing = SshCommand(
            (*command.argv_prefix, '-S', str(Path('/tmp') / f'zro-lease-mux-{os.getpid()}'))
        )
        master = SshCommand((*sharing.argv_prefix, '-M')).popen('experiment.invalid', 'cat')
        try:
            assert master.stdin is not None and master.stdout is not None
            master.stdin.write(b'master-handshake\n')
            master.stdin.flush()
            assert master.stdout.readline() == b'master-handshake\n', master.stderr_tail().decode()
            for abort in (False, True):
                results[f'mux-{abort}'] = exercise(sharing, scratch / f'mux-{abort}', abort=abort)
                unrelated = sharing.run('experiment.invalid', 'printf unrelated-operation')
                assert unrelated.stdout == b'unrelated-operation'
                assert master.poll() is None
            master.stdin.close()
            assert master.wait(timeout=30) == 0
        finally:
            _stop_process(master)
            Path('/tmp', f'zro-lease-mux-{os.getpid()}').unlink(missing_ok=True)
    (ROOT / 'TRANSPORT_CHECKED.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
