# SPDX-License-Identifier: Apache-2.0

"""Explicit runner-owned dynamic arguments, separate from literal strings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .tcl import tcl_quote


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


@dataclass(frozen=True)
class TclPathArgument:
    prefix: str
    path: str
    suffix: str = ""
    template: ArgumentTemplate | None = None

    def argument_template(self) -> ArgumentTemplate:
        parts = (self.path,) if self.template is None else self.template.parts
        if any(isinstance(part, TclWord) for part in parts):
            raise ValueError("a Tcl path cannot contain nested Tcl words")
        # Path templates contain only literal strings and session values.
        path_parts = tuple(part for part in parts if not isinstance(part, TclWord))
        return ArgumentTemplate((self.prefix, TclWord(path_parts), self.suffix))

    def render(self) -> str:
        return self.argument_template().preview()
