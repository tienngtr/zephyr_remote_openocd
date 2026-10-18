# SPDX-License-Identifier: Apache-2.0

"""Explicit runtime substitutions shared by planning and START decoding."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from zephyr_remote_openocd.protocol.tcl import tcl_quote


class SessionValue(Enum):
    WORKSPACE = "workspace"
    ADDRESS = "address"


@dataclass(frozen=True)
class TclWord:
    parts: tuple[str | SessionValue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        if not self.parts or not all(isinstance(part, (str, SessionValue)) for part in self.parts):
            raise ValueError("Tcl word must contain literal strings or session values")
        if any(isinstance(part, str) and "\0" in part for part in self.parts):
            raise ValueError("Tcl word literals must exclude NUL")


@dataclass(frozen=True)
class ArgumentTemplate:
    parts: tuple[str | SessionValue | TclWord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        if not self.parts or not all(
            isinstance(part, (str, SessionValue, TclWord)) for part in self.parts
        ):
            raise ValueError("argument template must contain explicit parts")
        if any(isinstance(part, str) and "\0" in part for part in self.parts):
            raise ValueError("argument template literals must exclude NUL")

    def preview(self) -> str:
        """Show unresolved values for offline planning, never infer substitution."""

        def text(part: str | SessionValue) -> str:
            return part if isinstance(part, str) else "{" + part.value + "}"

        return "".join(
            tcl_quote("".join(text(item) for item in part.parts), shield_placeholders=False)
            if isinstance(part, TclWord)
            else text(part)
            for part in self.parts
        )

    def wire_parts(self) -> list[Any]:
        def encode(part: str | SessionValue | TclWord) -> Any:
            if isinstance(part, SessionValue):
                return {"session": part.value}
            if isinstance(part, TclWord):
                return {"tcl_word": [encode(item) for item in part.parts]}
            return part

        return [encode(part) for part in self.parts]


def _render_parts(
    parts: Iterable[str | SessionValue | TclWord], replacements: Mapping[str, str]
) -> str:
    result = []
    for part in parts:
        if isinstance(part, str):
            result.append(part)
        elif isinstance(part, SessionValue):
            result.append(replacements[part.value])
        else:
            result.append(tcl_quote(_render_parts(part.parts, replacements)))
    return "".join(result)


def materialize_path(value: str | ArgumentTemplate, *, workspace: str, address: str) -> str:
    if isinstance(value, str):
        return value
    return _render_parts(value.parts, {"workspace": workspace, "address": address})


def materialize_argv(
    argv: Iterable[str],
    *,
    workspace: str,
    address: str,
    literal_prefix: int = 0,
    argv_templates: Iterable[tuple[int, ArgumentTemplate]] = (),
) -> tuple[str, ...]:
    """Resolve only explicit runner-owned templates, preserving all literal text.

    The helper reports this exact argv before spawning each child attempt so
    diagnostics never reconstruct session expansion independently.
    """
    replacements = {"workspace": workspace, "address": address}
    templates = dict(argv_templates)
    return tuple(
        _render_parts(templates[index].parts, replacements)
        if index >= literal_prefix and index in templates
        else arg
        for index, arg in enumerate(argv)
    )
