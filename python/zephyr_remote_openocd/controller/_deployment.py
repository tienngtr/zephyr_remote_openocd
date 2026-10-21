# SPDX-License-Identifier: Apache-2.0

"""Digest-based installation of the Protocol v1 helper."""

from __future__ import annotations

import hashlib
import io
import shlex
import subprocess
import zipfile
from dataclasses import dataclass
from importlib.resources import files

from zephyr_remote_openocd.controller._ssh import SshCommand
from zephyr_remote_openocd.protocol.responses import validate_deployment_response
from zephyr_remote_openocd.protocol.wire import ProtocolError, decode_frame


class DeploymentError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentResult:
    path: str
    sha256: str
    reused: bool


def bootstrap_program() -> str:
    """Return the packaged installation program sent to ``python3 -c``."""
    try:
        return (
            files("zephyr_remote_openocd")
            .joinpath("controller", "_deployment_bootstrap.py")
            .read_text(encoding="utf-8")
        )
    except OSError as error:
        raise DeploymentError(f"cannot read packaged helper bootstrap: {error}") from error


def build_helper_bundle() -> bytes:
    try:
        package = files("zephyr_remote_openocd")
        entries = {
            "__main__.py": package.joinpath("helper", "__main__.py").read_bytes(),
            "zephyr_remote_openocd/__init__.py": package.joinpath("__init__.py").read_bytes(),
        }
        for directory in ("helper", "protocol"):
            for module in package.joinpath(directory).iterdir():
                if module.name.endswith(".py"):
                    entries[f"zephyr_remote_openocd/{directory}/{module.name}"] = (
                        module.read_bytes()
                    )
        # Python executes a zip application regardless of filename extension.
        # Fixed metadata makes its digest depend only on canonical source bytes.
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in sorted(entries.items()):
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                bundle.writestr(info, data)
        return output.getvalue()
    except OSError as error:
        raise DeploymentError(f"cannot read packaged remote helper: {error}") from error


def deploy_helper(ssh: SshCommand, host: str, *, source: bytes | None = None) -> DeploymentResult:
    content = build_helper_bundle() if source is None else source
    program = bootstrap_program()
    try:
        result = ssh.run(host, "python3 -c " + shlex.quote(program), input_data=content, timeout=30)
    except subprocess.TimeoutExpired as error:
        raise DeploymentError(
            f"helper deployment timed out after {error.timeout:g} seconds"
        ) from error
    if result.returncode:
        diagnostic = result.stderr.decode("utf-8", "replace").strip()
        raise DeploymentError(f"helper deployment failed ({result.returncode}): {diagnostic}")
    try:
        message = decode_frame(result.stdout)
        validate_deployment_response(message)
        path = message["path"]
        digest = message["sha256"]
        if digest != hashlib.sha256(content).hexdigest():
            raise ValueError("remote helper digest differs from deployed source")
        return DeploymentResult(path, digest, message["status"] == "reused")
    except (KeyError, ProtocolError, TypeError, ValueError) as error:
        raise DeploymentError(f"invalid deployment response: {result.stdout!r}") from error
