# SPDX-License-Identifier: Apache-2.0

"""Protect the hardware thread-info oracle against generic thread matches."""

from __future__ import annotations

import pytest

from tests.acceptance.debug import assert_listed_threads

# Synthetic GDB ``info threads`` listings in the two forms GDB renders with
# OpenOCD Zephyr awareness. Only thread names count; frames reflect where each
# thread happened to stop and must not match.
QUOTED_NAMES = """\
  Id   Target Id                                      Frame
* 1    Thread 1 "idle" (Name: idle)                   idle ()
  2    Thread 2 "Philosopher 1" (Name: Philosopher 1) philosopher ()
  3    Thread 3 "Philosopher 0" (Name: Philosopher 0) philosopher ()
  4    Thread 4                                       main ()
"""
# The form shown by Zephyr's samples/philosophers documentation.
DESCRIBED_NAMES = """\
  Id   Target Id                                         Frame
* 1    Thread 1 (Name: idle, prio:15,useropts:1)         idle ()
  2    Thread 2 (Name: Philosopher 1, prio:2,useropts:4) philosopher ()
  3    Thread 3 (Name: Philosopher 0, prio:3,useropts:4) philosopher ()
  4    Thread 4                                          main ()
"""
LISTINGS = pytest.mark.parametrize(
    "listing", (QUOTED_NAMES, DESCRIBED_NAMES), ids=("quoted", "described")
)


@LISTINGS
def test_patterns_identifying_distinct_listed_threads_pass(listing: str) -> None:
    assert_listed_threads(listing, ("Philosopher 0", "Philosopher 1", "idle"))


@pytest.mark.parametrize(
    "patterns",
    (
        # An application thread that is not listed.
        ("Philosopher 0", "Philosopher 2"),
        # A pattern matching several names does not identify a thread.
        ("Philosopher [0-9]", "idle"),
        # Frame text and unnamed threads are not thread names.
        ("Philosopher 0", "philosopher"),
        ("Philosopher 0", "main"),
        # Two patterns naming the same thread prove only one thread.
        ("Philosopher 0", r"Philosopher\s0"),
    ),
)
@LISTINGS
def test_patterns_must_each_identify_a_different_listed_thread(
    listing: str, patterns: tuple[str, ...]
) -> None:
    with pytest.raises(AssertionError):
        assert_listed_threads(listing, patterns)
