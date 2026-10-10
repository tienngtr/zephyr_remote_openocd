# SPDX-License-Identifier: Apache-2.0

"""JSON-lines protocol shared by the client and remote helper."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any, BinaryIO

from .model import RemoteProcess, Service
from .services import validate_preferred_address
from .wire import EventOrder as EventOrder  # pylint: disable=useless-import-alias
from .wire import ProtocolError, decode_frame, encode_frame, validate_event

PROTOCOL_VERSION = 2
# The deployed helper is self-contained; keep its matching bound in sync.
MAX_CONTROL_FRAME_SIZE = 1024 * 1024
SHA256_HEX_DIGEST_LENGTH = 64
_ENVELOPE_FIELDS = frozenset(("version", "type"))


def is_protocol_version(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == PROTOCOL_VERSION


def encode_message(message_type: str, **fields: Any) -> bytes:
    return encode_frame(message_type, **fields)


def decode_message(line: bytes | str) -> dict[str, Any]:
    return decode_frame(line)


def decode_single_frame(frame: bytes | str) -> dict[str, Any]:
    return decode_frame(frame)


def read_message(stream: BinaryIO) -> dict[str, Any]:
    line = stream.readline(MAX_CONTROL_FRAME_SIZE + 1)
    if not line:
        raise EOFError("helper control channel closed")
    return decode_single_frame(line)


def _write_frame(stream: BinaryIO, message_type: str, **fields: Any) -> None:
    frame = encode_message(message_type, **fields)
    if len(frame) > MAX_CONTROL_FRAME_SIZE:
        raise ProtocolError(
            f"control frame exceeds maximum size of {MAX_CONTROL_FRAME_SIZE} bytes including LF"
        )
    stream.write(frame)
    stream.flush()


def write_start(
    stream: BinaryIO,
    process: RemoteProcess,
    services: Iterable[Service],
    *,
    preferred_address: str | None = None,
) -> None:
    """Serialize a validated process and service model as the START command."""

    validate_preferred_address(preferred_address)
    _write_frame(
        stream,
        "START",
        completion_policy=process.completion_policy.value,
        preferred_address=preferred_address,
        argv=list(process.argv),
        environment=dict(process.environment),
        required_paths=[
            {
                "kind": check.kind,
                "path": check.path
                if check.template is None
                else {"parts": check.template.wire_parts()},
            }
            for check in process.required_paths
        ],
        services=[
            {"name": service.name, "remote_port": service.remote_port} for service in services
        ],
        required_output_sentinels=list(process.required_output_sentinels),
        readiness_timeout=process.readiness_timeout,
        literal_prefix=process.literal_prefix,
        argv_templates=[
            {"index": index, "parts": template.wire_parts()}
            for index, template in process.argv_templates
        ],
    )


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _has_exact_fields(message: dict[str, Any], fields: frozenset[str]) -> bool:
    return set(message) == _ENVELOPE_FIELDS | fields


_STAGED_FIELDS = frozenset(("byte_count", "sha256", "files", "directories"))
_OPENOCD_VERSION_FIELDS = frozenset(("output",))
_DEPLOYMENT_FIELDS = frozenset(("status", "path", "sha256"))


def validate_helper_event(message: dict[str, Any]) -> None:
    validate_event(message)


def validate_error_response(message: dict[str, Any]) -> None:
    if (
        not _has_exact_fields(message, frozenset(("code", "message")))
        or message.get("type") != "ERROR"
        or not _non_empty_string(message.get("code"))
        or not isinstance(message.get("message"), str)
    ):
        raise ProtocolError("invalid standalone error response")


def validate_staged_response(message: dict[str, Any]) -> None:
    if not isinstance(message, dict):
        raise ProtocolError("invalid STAGED response")
    files = message.get("files")
    directories = message.get("directories")
    if (
        not _has_exact_fields(message, _STAGED_FIELDS)
        or message.get("type") != "STAGED"
        or not isinstance(message.get("byte_count"), int)
        or isinstance(message.get("byte_count"), bool)
        or message["byte_count"] < 0
        or not _sha256(message.get("sha256"))
        or not isinstance(files, list)
        or not isinstance(directories, list)
        or not all(_valid_manifest_path(item) for item in (*files, *directories))
        or len(set(files)) != len(files)
        or len(set(directories)) != len(directories)
        or set(files) & set(directories)
        or _manifest_has_file_ancestor(files, directories)
    ):
        raise ProtocolError("invalid STAGED response")


def _valid_manifest_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and str(path) == value
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _manifest_has_file_ancestor(files: list[str], directories: list[str]) -> bool:
    file_paths = {PurePosixPath(value) for value in files}
    all_paths = tuple(PurePosixPath(value) for value in (*files, *directories))
    return any(any(parent in file_paths for parent in path.parents) for path in all_paths)


def validate_openocd_version_response(message: dict[str, Any]) -> None:
    if (
        not isinstance(message, dict)
        or not _has_exact_fields(message, _OPENOCD_VERSION_FIELDS)
        or message.get("type") != "OPENOCD_VERSION"
        or not isinstance(message.get("output"), str)
    ):
        raise ProtocolError("invalid OPENOCD_VERSION response")


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_HEX_DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_deployment_response(message: dict[str, Any]) -> None:
    if (
        not isinstance(message, dict)
        or not _has_exact_fields(message, _DEPLOYMENT_FIELDS)
        or message.get("type") != "DEPLOYED"
        or message.get("status") not in {"deployed", "reused"}
        or not _non_empty_string(message.get("path"))
        or not _sha256(message.get("sha256"))
    ):
        raise ProtocolError("invalid deployment response")
