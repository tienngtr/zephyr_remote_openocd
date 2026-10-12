# SPDX-License-Identifier: Apache-2.0

"""Planning of Tcl path words with explicit runtime substitutions."""

from __future__ import annotations

from dataclasses import dataclass

from zephyr_remote_openocd.protocol.arguments import ArgumentTemplate, TclWord


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
