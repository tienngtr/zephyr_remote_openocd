# SPDX-License-Identifier: Apache-2.0

"""Digest-based installation of the Protocol v1 helper."""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from importlib.resources import files

from .protocol import ProtocolError, decode_message, validate_deployment_response
from .ssh import SshCommand

BOOTSTRAP = r'''import fcntl,hashlib,json,os,pathlib,sys,tempfile,time
data=sys.stdin.buffer.read()
digest=hashlib.sha256(data).hexdigest()
base=pathlib.Path.home()/'.local/libexec/zephyr_remote_openocd/protocol_v1'
base.mkdir(mode=0o700,parents=True,exist_ok=True)
os.chmod(base,0o700)
with (base/'.deploy.lock').open('a+b') as lock:
    os.fchmod(lock.fileno(),0o600)
    fcntl.flock(lock,fcntl.LOCK_EX)
    target=base/('helper-'+digest+'.py')
    reused=False
    try:
        reused=target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==digest
    except OSError:
        reused=False
    if not reused:
        fd,tmp=tempfile.mkstemp(prefix='.helper_',dir=base)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'wb') as out:
                out.write(data);out.flush();os.fsync(out.fileno())
            os.replace(tmp,target)
        finally:
            try: os.unlink(tmp)
            except FileNotFoundError: pass
    os.chmod(target,0o600)
    os.utime(target,None)
    cutoff=time.time()-24*60*60
    for candidate in base.glob('helper-*.py'):
        if candidate==target:
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            pass
print(json.dumps({'version':1,'type':'DEPLOYED','status':'reused' if reused else 'deployed',
                  'path':str(target.resolve()),'sha256':digest}))
'''


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentResult:
    path: str
    sha256: str
    reused: bool


def _helper_source() -> bytes:
    try:
        return files("zephyr_remote_openocd").joinpath("remote_helper.py").read_bytes()
    except OSError as error:
        raise DeploymentError(f"cannot read packaged remote helper: {error}") from error


def deploy_helper(ssh: SshCommand, host: str, *, source: bytes | None = None) -> DeploymentResult:
    content = _helper_source() if source is None else source
    result = ssh.run(host, "python3 -c " + shlex.quote(BOOTSTRAP), input_data=content, timeout=30)
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", "replace").strip()
        raise DeploymentError(f"helper deployment failed ({result.returncode}): {diagnostic}")
    try:
        message = decode_message(result.stdout)
        validate_deployment_response(message)
        path = message["path"]
        digest = message["sha256"]
        if digest != hashlib.sha256(content).hexdigest():
            raise ValueError("remote helper digest differs from deployed source")
        return DeploymentResult(path, digest, message["status"] == "reused")
    except (KeyError, ProtocolError, TypeError, ValueError) as error:
        raise DeploymentError(f"invalid deployment response: {result.stdout!r}") from error
