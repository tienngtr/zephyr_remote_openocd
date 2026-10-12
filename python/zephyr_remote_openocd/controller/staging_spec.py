# SPDX-License-Identifier: Apache-2.0

"""Immutable local staging inputs and destination validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


def validated_destination(value: str | PurePosixPath) -> PurePosixPath:
    path = PurePosixPath(value)
    if not str(path) or str(path) == "." or path.is_absolute():
        raise ValueError(f"staged destination must be a non-empty relative path: {value!s}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"staged destination is not normalized: {value!s}")
    return path


@dataclass(frozen=True)
class StagedFile:
    source: Path
    destination: PurePosixPath

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Path(self.source))
        object.__setattr__(self, "destination", validated_destination(self.destination))


@dataclass(frozen=True)
class StagedDirectory:
    source: Path
    destination: PurePosixPath

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Path(self.source))
        object.__setattr__(self, "destination", validated_destination(self.destination))


StagedEntry = StagedFile | StagedDirectory
