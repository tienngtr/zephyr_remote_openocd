"""Digest-based installation of the protocol-1 helper."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shlex

from .ssh import SshCommand

PROTOCOL_VERSION = 1
HELPER_SOURCE = Path(__file__).resolve().parents[1] / "remote_helper.py"

BOOTSTRAP = r'''import hashlib,json,os,pathlib,sys,tempfile
data=sys.stdin.buffer.read()
digest=hashlib.sha256(data).hexdigest()
base=pathlib.Path.home()/'.local/libexec/zephyr-remote-openocd/protocol-1'
base.mkdir(mode=0o700,parents=True,exist_ok=True)
os.chmod(base,0o700)
target=base/'helper.py'
reused=False
try:
    reused=target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==digest
except OSError:
    reused=False
if not reused:
    fd,tmp=tempfile.mkstemp(prefix='.helper-',dir=base)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as out:
            out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(tmp,target)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
os.chmod(target,0o600)
print(json.dumps({'version':1,'type':'DEPLOYED','status':'reused' if reused else 'deployed','path':str(target.resolve()),'sha256':digest}))
'''


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentResult:
    path: str
    sha256: str
    reused: bool


def deploy_helper(ssh: SshCommand, host: str, *, source: bytes | None = None) -> DeploymentResult:
    content = HELPER_SOURCE.read_bytes() if source is None else source
    result = ssh.run(host, "python3 -c " + shlex.quote(BOOTSTRAP), input_data=content, timeout=30)
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", "replace").strip()
        raise DeploymentError(f"helper deployment failed ({result.returncode}): {diagnostic}")
    try:
        message = json.loads(result.stdout)
        if message.get("version") != PROTOCOL_VERSION or message.get("type") != "DEPLOYED":
            raise ValueError("unexpected deployment response")
        path = message["path"]
        digest = message["sha256"]
        if digest != hashlib.sha256(content).hexdigest():
            raise ValueError("remote helper digest differs from deployed source")
        return DeploymentResult(path, digest, message["status"] == "reused")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DeploymentError(f"invalid deployment response: {result.stdout!r}") from error
