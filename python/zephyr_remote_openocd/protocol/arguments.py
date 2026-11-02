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
    LOG_SPOOL = "log_spool"


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


def log_spool_template() -> ArgumentTemplate:
    """Return the only template form that may reference the session log channel."""
    return ArgumentTemplate((TclWord((SessionValue.LOG_SPOOL,)),))


def _references_log_spool(parts: Iterable[str | SessionValue | TclWord]) -> bool:
    return any(
        part is SessionValue.LOG_SPOOL
        or isinstance(part, TclWord)
        and SessionValue.LOG_SPOOL in part.parts
        for part in parts
    )


def log_spool_requested(
    argv_templates: Iterable[ArgumentTemplate], path_templates: Iterable[ArgumentTemplate]
) -> bool:
    """Validate log-channel references; their exact argv form requests the channel."""
    if any(_references_log_spool(template.parts) for template in path_templates):
        raise ValueError("required paths cannot reference the log channel")
    references = [template for template in argv_templates if _references_log_spool(template.parts)]
    if any(template != log_spool_template() for template in references):
        raise ValueError("the log channel is valid only as one complete Tcl-word argument")
    if len(references) > 1:
        raise ValueError("at most one argument may reference the log channel")
    return bool(references)


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
    # Required paths cannot reference the log channel; START validation rejects it.
    return _render_parts(value.parts, {"workspace": workspace, "address": address})


def materialize_argv(
    argv: Iterable[str],
    *,
    workspace: str,
    address: str,
    literal_prefix: int = 0,
    argv_templates: Iterable[tuple[int, ArgumentTemplate]] = (),
    log_spool: str | None = None,
) -> tuple[str, ...]:
    """Resolve only explicit runner-owned templates, preserving all literal text.

    The helper reports this exact argv before spawning each child attempt so
    diagnostics never reconstruct session expansion independently.
    """
    replacements = {"workspace": workspace, "address": address}
    if log_spool is not None:
        replacements["log_spool"] = log_spool
    templates = dict(argv_templates)
    return tuple(
        _render_parts(templates[index].parts, replacements)
        if index >= literal_prefix and index in templates
        else arg
        for index, arg in enumerate(argv)
    )
