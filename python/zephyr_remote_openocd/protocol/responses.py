# SPDX-License-Identifier: Apache-2.0

"""Validation of standalone helper responses and staging manifests."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from zephyr_remote_openocd.protocol.wire import ProtocolError

SHA256_HEX_DIGEST_LENGTH = 64
_ENVELOPE_FIELDS = frozenset(("version", "type"))
_STAGED_FIELDS = frozenset(("byte_count", "sha256", "files", "directories"))
_OPENOCD_VERSION_FIELDS = frozenset(("output",))
_DEPLOYMENT_FIELDS = frozenset(("status", "path", "sha256"))


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _has_exact_fields(message: dict[str, Any], fields: frozenset[str]) -> bool:
    return set(message) == _ENVELOPE_FIELDS | fields


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


def _valid_manifest_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and str(path) == value
        # PurePosixPath(".") has no parts, so inspect the literal components.
        and all(part not in ("", ".", "..") for part in value.split("/"))
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


def _sha256(value: object) -> bool:
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
