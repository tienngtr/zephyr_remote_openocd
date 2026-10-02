# SPDX-License-Identifier: Apache-2.0

"""Generated Tcl arguments whose paths depend on session allocation."""

from dataclasses import dataclass


def tcl_quote(value: str, *, shield_placeholders: bool = True) -> str:
    """Quote one Tcl word, also shielding braces from later helper expansion."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for character in "$[]" + ("{}" if shield_placeholders else ""):
        escaped = escaped.replace(character, "\\" + character)
    return f'"{escaped}"'


@dataclass(frozen=True)
class TclPathArgument:
    """Keep an owned path separate from opaque surrounding Tcl text."""

    prefix: str
    path: str
    suffix: str = ""

    def render(self, workspace: str | None = None) -> str:
        if workspace is None:
            return self.prefix + tcl_quote(self.path, shield_placeholders=False) + self.suffix
        # Only address tokens in the planned path remain deferred to the helper.
        # Placeholder-like text introduced by workspace allocation is literal.
        parts = (part.replace("{workspace}", workspace) for part in self.path.split("{address}"))
        quoted = '"' + "{address}".join(tcl_quote(part)[1:-1] for part in parts) + '"'
        return self.prefix + quoted + self.suffix
