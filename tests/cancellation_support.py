# SPDX-License-Identifier: Apache-2.0

"""Deliver a real signal inside the GC callback that can discard exceptions."""

import gc
import signal
import weakref


class _CyclicVictim:
    def __init__(self) -> None:
        self.cycle = self


def deliver_sigint_in_gc() -> None:
    """Exercise the unraisable boundary with cyclic GC enabled."""
    assert gc.isenabled(), "production cancellation must not depend on disabling GC"
    victim = _CyclicVictim()
    delivered = []

    def collected(_reference):
        signal.raise_signal(signal.SIGINT)
        delivered.append(True)

    reference = weakref.ref(victim, collected)
    del victim
    gc.collect()
    assert reference() is None
    assert delivered, "a raising handler lost cancellation inside the weakref callback"
