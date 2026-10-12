# SPDX-License-Identifier: Apache-2.0

"""Composition of independent helper cleanup failures."""

from __future__ import annotations


def _raise_cleanup_errors(errors):
    """Raise the first cleanup error after retaining subsequent diagnostics."""
    if not errors:
        return
    first, *additional = errors
    for error in additional:
        first.add_note(f"additional cleanup failure: {error}")
        for note in tuple(getattr(error, "__notes__", ())):
            first.add_note(f"additional cleanup failure detail: {note}")
    raise first
