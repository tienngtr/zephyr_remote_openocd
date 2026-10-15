# SPDX-License-Identifier: Apache-2.0

"""Operation-owned durable Ctrl-C capture and cancellation boundaries.

The signal handler only latches one monotonic fact. Native delivery also writes
an uninterpreted byte to a dedicated wakeup descriptor so a main-thread wait can
observe it even before the Python-level handler runs. Cancellation becomes
application control flow only where the operation explicitly dispatches it.
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from types import FrameType

from zephyr_remote_openocd.controller._cleanup import add_failure_note, raise_cleanup_errors

_SignalHandler = Callable[[int, FrameType | None], object] | int | None


class OperationCancelled(BaseException):
    """The operation's single internal cancellation identity.

    It derives from ``BaseException`` so ordinary ``except Exception`` handling
    cannot absorb it. The runner projects it to ``KeyboardInterrupt`` only at its
    outer boundary, after settlement and capture restoration.
    """


class OperationCancellation:
    """Own one operation's monotonic cancellation fact and native capture.

    ``not requested -> requested`` never resets. Capture is installed only on
    the main thread, where CPython runs Python signal handlers, and only when
    SIGINT currently dispatches to a Python handler. Ignored or native-default
    dispositions are preserved without capture.
    """

    def __init__(self) -> None:
        self._requested = False
        self._closing = False
        self._client_owns_sigint = False
        self.cancellation = OperationCancelled("operation cancelled by Ctrl-C")
        self._wakeup_fds: tuple[int, int] | None = None
        self._previous_handler: _SignalHandler = None
        self._previous_wakeup_fd = -1
        self._handler_installed = False
        self._wakeup_installed = False

    def _latch(self, signum: int, frame: FrameType | None) -> None:
        """Python SIGINT handler: record the fact only; never raise or allocate.

        Delivery while an interactive client owns SIGINT is not runner
        cancellation, however late the runtime dispatches it.
        """
        del signum, frame
        if not self._client_owns_sigint:
            self._requested = True

    @property
    def requested(self) -> bool:
        return self._requested

    @property
    def capturing(self) -> bool:
        return self._handler_installed

    @property
    def native_wakeup_fd(self) -> int | None:
        """Readable descriptor woken by native signal delivery, if captured."""
        if self._wakeup_fds is None or not self._wakeup_installed:
            return None
        return self._wakeup_fds[0]

    def drain_native_wakeup(self) -> None:
        """Consume notification hints; their values are never interpreted."""
        descriptor = self.native_wakeup_fd
        if descriptor is None:
            return
        try:
            while os.read(descriptor, 4096):
                pass
        except BlockingIOError:
            pass

    def dispatch(self) -> None:
        """Raise the cancellation at an active wait or admission boundary.

        Closure has already accounted for cancellation and settles every
        independent resource, so repeated Ctrl-C does not dispatch again.
        """
        if self._requested and not self._closing:
            raise self.cancellation

    def begin_closure(self) -> bool:
        """Enter settlement and report whether cancellation initiated closure."""
        first = not self._closing
        self._closing = True
        return first and self._requested

    @contextmanager
    def interactive_client(self) -> Generator[None, None, None]:
        """Transfer terminal Ctrl-C ownership to an interactive client interval.

        Ownership transfers when ``_client_owns_sigint`` is set: a Ctrl-C the
        runner latched before then refuses the launch at the following re-read;
        any later delivery belongs to the client. SIGINT is then ignored so the
        client and wrappers inherit stock Zephyr's disposition. Exit restores
        non-raising capture and returns ownership before further runner work.
        """
        if not self._handler_installed:
            self.dispatch()
            yield
            self.dispatch()
            return
        self._client_owns_sigint = True
        primary: BaseException | None = None
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            self.dispatch()
            yield
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                signal.signal(signal.SIGINT, self._latch)
            except BaseException as error:
                if primary is None:
                    raise
                add_failure_note(primary, "runner Ctrl-C capture restoration also failed", error)
            finally:
                self._client_owns_sigint = False
        self.dispatch()

    def _install(self) -> None:
        """Install native capture as an ownership transition on the main thread.

        Restoration responsibility is recorded before each native mutation.
        The non-raising handler is installed first, so a Ctrl-C delivered to
        the previous handler cannot interrupt the remaining steps.
        """
        if threading.current_thread() is not threading.main_thread():
            return
        previous_handler = signal.getsignal(signal.SIGINT)
        if not callable(previous_handler):
            return
        self._previous_handler = previous_handler
        self._handler_installed = True
        signal.signal(signal.SIGINT, self._latch)
        self._wakeup_fds = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._previous_wakeup_fd = signal.set_wakeup_fd(
            self._wakeup_fds[1], warn_on_full_buffer=False
        )
        self._wakeup_installed = True

    def _restore(self) -> None:
        """Restore prior capture, then release descriptors CPython no longer uses.

        The wakeup slot is inspected rather than trusted to a flag: the prior
        value is restored only if this operation's descriptor holds the slot,
        and the descriptor is closed only after the slot no longer refers to it.
        """
        errors: list[BaseException] = []
        fds = self._wakeup_fds
        self._wakeup_installed = False
        slot_released = fds is None
        if fds is not None:
            try:
                current = signal.set_wakeup_fd(-1)
                signal.set_wakeup_fd(self._previous_wakeup_fd if current == fds[1] else current)
                slot_released = True
            except BaseException as error:
                errors.append(error)
        if self._handler_installed:
            try:
                signal.signal(signal.SIGINT, self._previous_handler)
                self._handler_installed = False
            except BaseException as error:
                errors.append(error)
        if fds is not None and slot_released:
            for descriptor in fds:
                try:
                    os.close(descriptor)
                except BaseException as error:
                    errors.append(error)
            self._wakeup_fds = None
        raise_cleanup_errors(errors)


_ACTIVE = threading.local()


def current_operation() -> OperationCancellation | None:
    """Return the operation owned by this thread, if any."""
    return getattr(_ACTIVE, "operation", None)


def admit_effect() -> None:
    """Final dependent-effect admission: refuse new work after cancellation."""
    operation = current_operation()
    if operation is not None:
        operation.dispatch()


@contextmanager
def operation_scope() -> Generator[OperationCancellation, None, None]:
    """Own native capture for one live operation, or borrow an active owner.

    The owner is the final outer operation boundary: after nested settlement
    and capture restoration it dispatches a cancellation that no earlier
    boundary consumed, and keeps a concurrent primary failure intact.
    """
    existing = current_operation()
    if existing is not None:
        yield existing
        return
    operation = OperationCancellation()
    _ACTIVE.operation = operation
    primary: BaseException | None = None
    try:
        operation._install()
        yield operation
    except BaseException as error:
        primary = error
        raise
    finally:
        _ACTIVE.operation = None
        try:
            operation._restore()
        except BaseException as error:
            if primary is None:
                raise
            add_failure_note(primary, "runner Ctrl-C capture restoration also failed", error)
        if primary is not None and operation.requested and primary is not operation.cancellation:
            primary.add_note("operation cancellation (Ctrl-C) was also requested")
    if operation.requested:
        raise operation.cancellation
