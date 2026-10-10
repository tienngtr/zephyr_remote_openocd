# SPDX-License-Identifier: Apache-2.0
"""Real pipes, production SshCommand, and optional local OpenSSH inetd feasibility.

Keys/configuration/receipts are generated only in ignored scratch space. The
sshd uses ProxyCommand/inetd pipes, not a lab host or a network listener.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import selectors
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import IO

from zephyr_remote_openocd.remote.ssh import SshCommand, _stop_process

ROOT = Path(__file__).resolve().parent


def line(stream: IO[bytes]) -> bytes:
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        if not selector.select(30):
            raise TimeoutError('experiment handshake did not arrive')
        result = stream.readline()
        if not result:
            raise EOFError('experiment pipe ended before handshake')
        return result


def run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, capture_output=True, check=True, timeout=30)


def exercise(
    command: SshCommand, receipt: Path, *, abort: bool, other_operation: bool = False
) -> dict[str, bool | int]:
    os.mkfifo(receipt, 0o600)
    watch = subprocess.Popen(
        [sys.executable, '-u', str(ROOT / 'transport_peer.py'), 'watch', str(receipt)],
        stdout=subprocess.PIPE,
    )
    managed = None
    try:
        assert watch.stdout is not None and line(watch.stdout) == b'WATCHING\n'
        remote = shlex.join(
            [sys.executable, '-u', str(ROOT / 'transport_peer.py'), 'peer', str(receipt)]
        )
        managed = command.popen('experiment.invalid', remote)
        assert managed.stdin is not None and managed.stdout is not None
        assert line(managed.stdout) == b'SESSION_CREATED\n'
        managed.stdin.write(b'START\n')
        managed.stdin.flush()
        assert line(managed.stdout) == b'START_ACCEPTED\n'
        if other_operation:
            other = command.run('experiment.invalid', 'printf unrelated-operation')
            assert other.returncode == 0 and other.stdout == b'unrelated-operation'
        if abort:
            managed.kill()  # unexpected local SSH/pipe process death
            managed.wait(timeout=30)
        else:
            managed.stdin.close()  # leave stdout and the managed stderr reader alive
            terminal = json.loads(line(managed.stdout))
            assert terminal == {'type': 'SESSION_ENDED', 'cause': 'controller_ended'}
            assert managed.wait(timeout=30) == 0
            tail = managed.stderr_tail()
            assert b'diagnostic before EOF' in tail and b'diagnostic after EOF' in tail
        assert line(watch.stdout) == b'CLEANED\n'
        assert watch.wait(timeout=30) == 0
        return {
            'cleanup_receipt': True,
            'final_received': not abort,
            'intentional': not abort,
            'other_operation_during_lease': other_operation,
        }
    finally:
        if managed is not None:
            _stop_process(managed)
        if watch.poll() is None:
            watch.kill()
        watch.wait(timeout=30)
        if watch.stdout is not None:
            watch.stdout.close()
        receipt.unlink(missing_ok=True)


def local_command(scratch: Path) -> SshCommand:
    # This fixed configured command preserves the real production process/drain
    # acquisition path. Its last two argv values have SshCommand host/command shape.
    proxy = scratch / 'pipe_proxy.py'
    proxy.write_text(
        'import shlex,subprocess,sys\n'
        'peer=subprocess.Popen(shlex.split(sys.argv[-1]),stdin=subprocess.PIPE)\n'
        'for frame in sys.stdin.buffer:\n'
        ' peer.stdin.write(frame);peer.stdin.flush()\n'
        'peer.stdin.close()\npeer.wait()\n'
    )
    return SshCommand((sys.executable, str(proxy)))


def ssh_command(scratch: Path, ssh: str, sshd: str, keygen: str) -> SshCommand:
    for name in ('host_key', 'client_key'):
        run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(scratch / name)])
    authorized = scratch / 'authorized_keys'
    authorized.write_text((scratch / 'client_key.pub').read_text())
    authorized.chmod(0o600)
    server = scratch / 'sshd_config'
    server.write_text(
        f'HostKey {scratch / "host_key"}\nAuthorizedKeysFile {authorized}\n'
        'StrictModes no\nUsePAM no\nPasswordAuthentication no\n'
        'KbdInteractiveAuthentication no\nPubkeyAuthentication yes\n'
        'PermitRootLogin no\nLogLevel DEBUG1\n'
    )
    known = scratch / 'known_hosts'
    known.write_text('experiment.invalid ' + (scratch / 'host_key.pub').read_text())
    client = scratch / 'ssh_config'
    proxy = shlex.join([sshd, '-i', '-f', str(server), '-E', str(scratch / 'sshd.log')])
    client.write_text(
        'Host experiment.invalid\n'
        f' User {getpass.getuser()}\n IdentityFile {scratch / "client_key"}\n'
        f' UserKnownHostsFile {known}\n ProxyCommand {proxy}\n'
        ' BatchMode yes\n IdentitiesOnly yes\n StrictHostKeyChecking yes\n'
        ' RequestTTY no\n ForwardAgent no\n'
    )
    return SshCommand((ssh, '-F', str(client), '-T'))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--openssh', action='store_true')
    args = parser.parse_args()
    scratch = ROOT.parents[2] / '.scratch/agents/product-semantics/transport'
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, mode=0o700)
    results: dict[str, object] = {}
    pipe = local_command(scratch)
    for abort in (False, True):
        results[f'pipe-{abort}'] = exercise(pipe, scratch / 'receipt', abort=abort)
    if args.openssh:
        ssh, sshd, keygen = (shutil.which(name) for name in ('ssh', 'sshd', 'ssh-keygen'))
        assert ssh and sshd and keygen, 'OpenSSH client/server/keygen are required'
        command = ssh_command(scratch, ssh, sshd, keygen)
        results['openssh_version'] = run([ssh, '-V']).stderr.decode().strip()
        for abort in (False, True):
            results[f'ssh-{abort}'] = exercise(command, scratch / 'receipt', abort=abort)
        sharing = SshCommand((*command.argv_prefix, '-S', str(scratch / 'mux')))
        master_command = SshCommand((*sharing.argv_prefix, '-M'))
        hold = shlex.join([sys.executable, '-u', str(ROOT / 'transport_peer.py'), 'hold', '-'])
        master = master_command.popen('experiment.invalid', hold)
        try:
            assert master.stdout is not None and master.stdin is not None
            assert line(master.stdout) == b'MASTER_READY\n'
            for abort in (False, True):
                results[f'mux-{abort}'] = exercise(
                    sharing, scratch / 'receipt', abort=abort, other_operation=True
                )
                assert master.poll() is None
                run([*sharing.argv_prefix, '-O', 'check', 'experiment.invalid'])
            master.stdin.close()
            assert line(master.stdout) == b'MASTER_ENDED\n'
            assert master.wait(timeout=30) == 0
        finally:
            _stop_process(master)
    (ROOT / 'TRANSPORT_CHECKED.md').write_text(
        '# Checked transport results\n\n```json\n' + json.dumps(results, indent=2) + '\n```\n'
    )
    print('Verified transport half-close and independent cleanup receipts.')


if __name__ == '__main__':
    main()
