# SPDX-License-Identifier: Apache-2.0

"""External-test cleanup scopes preserve failures and attempt every owner."""

from contextlib import ExitStack

import pytest

from tests.process_support import cleanup_on_exit


@pytest.mark.parametrize(
    "failure, failed_cleanups",
    (
        (None, ()),
        ("second-prepare", ("first",)),
        ("second-acquire", ("second",)),
        (None, ("second",)),
        (None, ("first", "second")),
        ("assertion", ("first", "second")),
        ("interrupt", ("first", "second")),
    ),
)
def test_cleanup_scopes_attempt_all_owners_and_preserve_primary(
    failure: str | None, failed_cleanups: tuple[str, ...]
) -> None:
    attempted: list[str] = []
    errors = {name: RuntimeError(f"{name} cleanup failed") for name in failed_cleanups}
    for error in errors.values():
        error.add_note("nested cleanup diagnostic")
    primary: BaseException | None = None
    if failure == "assertion":
        primary = AssertionError("operation assertion failed")
    elif failure == "interrupt":
        primary = KeyboardInterrupt()
    elif failure is not None:
        primary = RuntimeError("resource acquisition failed")

    def cleanup(name: str) -> None:
        attempted.append(name)
        if name in errors:
            raise errors[name]

    def operation() -> None:
        with ExitStack() as owners:
            owners.enter_context(cleanup_on_exit(lambda: cleanup("first")))
            if failure == "second-prepare":
                assert primary is not None
                raise primary
            owners.enter_context(cleanup_on_exit(lambda: cleanup("second")))
            if primary is not None:
                raise primary

    expected = primary if primary is not None else errors.get("second")
    if expected is None:
        operation()
    else:
        with pytest.raises(type(expected)) as raised:
            operation()
        assert raised.value is expected
        for error in errors.values():
            if error is not expected:
                assert any(str(error) in note for note in expected.__notes__)
        if errors:
            assert any("nested cleanup diagnostic" in note for note in expected.__notes__)
    assert set(attempted) == ({"first"} if failure == "second-prepare" else {"first", "second"})
    assert len(attempted) == len(set(attempted))
