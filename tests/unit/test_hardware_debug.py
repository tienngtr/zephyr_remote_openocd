# SPDX-License-Identifier: Apache-2.0

"""Protect the hardware thread-info oracle against transcript-wide matches."""

from __future__ import annotations

import pytest

from tests.acceptance.debug import assert_listed_threads

# Representative ``west attach`` output with OpenOCD Zephyr thread awareness.
TRANSCRIPT = """\
Info : Zephyr: thread_info_offsets loaded, thread list on target
ZRO_THREADS_BEGIN
  Id   Target Id                                                Frame
* 1    Thread 536871400 "idle" (Name: idle, State: pending)     arch_cpu_idle ()
  2    Thread 536871624 "Philosopher 0" (Name: Philosopher 0)   z_swap ()
  3    Thread 536871848 "Philosopher 1" (Name: Philosopher 1)   z_swap ()
ZRO_THREADS_END
[Inferior 1 (Remote target) detached]
"""


def test_patterns_identifying_distinct_listed_threads_pass() -> None:
    assert_listed_threads(TRANSCRIPT, ("Philosopher 0", "Philosopher 1", r"\bidle\b"))


@pytest.mark.parametrize(
    ("patterns", "diagnostic"),
    (
        # Found only in OpenOCD output outside the GDB thread list.
        (("thread list on target", "Philosopher 0"), "matched 0 thread rows"),
        # Generic text present on every row does not identify a thread.
        (("Thread", "Philosopher 0"), "matched 3 thread rows"),
        (("Philosopher 0", "Name: Philosopher 0"), "share a row"),
    ),
)
def test_patterns_must_each_identify_a_different_listed_thread(patterns, diagnostic) -> None:
    with pytest.raises(AssertionError, match=diagnostic):
        assert_listed_threads(TRANSCRIPT, patterns)
