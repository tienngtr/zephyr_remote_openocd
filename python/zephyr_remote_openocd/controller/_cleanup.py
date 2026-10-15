# SPDX-License-Identifier: Apache-2.0

"""Internal helpers for composing cleanup failures."""


def add_failure_note(
    primary: BaseException,
    prefix: str,
    secondary: BaseException,
) -> None:
    """Retain an exception and its existing diagnostics on another failure."""
    if primary is secondary:
        return
    primary.add_note(f"{prefix}: {secondary}")
    for note in tuple(getattr(secondary, "__notes__", ())):
        primary.add_note(f"{prefix} detail: {note}")


def raise_cleanup_errors(errors: list[BaseException]) -> None:
    """Raise the first cleanup error after retaining subsequent diagnostics."""
    if not errors:
        return
    first, *additional = errors
    for error in additional:
        add_failure_note(first, "additional cleanup failure", error)
    raise first
