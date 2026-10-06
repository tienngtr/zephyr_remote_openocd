# SPDX-License-Identifier: Apache-2.0

"""Literal Tcl-word quoting for planning previews."""


def tcl_quote(value: str, *, shield_placeholders: bool = True) -> str:
    """Quote one Tcl word; previews may leave display-only braces unescaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    for character in "$[]" + ("{}" if shield_placeholders else ""):
        escaped = escaped.replace(character, "\\" + character)
    return f'"{escaped}"'
