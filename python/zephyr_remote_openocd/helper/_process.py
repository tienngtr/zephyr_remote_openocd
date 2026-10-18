# SPDX-License-Identifier: Apache-2.0

"""OpenOCD child acquisition, process-group ownership, and settlement."""

from __future__ import annotations

import asyncio
import io
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, TYPE_CHECKING, NamedTuple, Protocol

from zephyr_remote_openocd.helper._child_output import OutputDecoder, RequiredOutputSentinels
from zephyr_remote_openocd.helper._cleanup import raise_cleanup_errors
from zephyr_remote_openocd.helper._streams import AsyncInput, readable
from zephyr_remote_openocd.protocol.arguments import materialize_path
from zephyr_remote_openocd.protocol.commands import RequiredPath, StartRequest

if TYPE_CHECKING:
    from zephyr_remote_openocd.helper._observations import ObservationFailed


CHILD_TERM_TIMEOUT = 5
CHILD_POLL_INTERVAL = 0.05
CHILD_REAP_TIMEOUT = 1
CHILD_GROUP_EXIT_TIMEOUT = 1


class ChildProcessHandle(Protocol):
    """Leader identity, captured pipes, and reaping at the OS process boundary."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdout(self) -> IO[bytes] | None: ...

    @property
    def stderr(self) -> IO[bytes] | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _group_exit_waits(pid: int) -> Iterator[float]:
    """Poll group disappearance after reaping, without signalling or enumerating."""
    deadline = time.monotonic() + CHILD_GROUP_EXIT_TIMEOUT
    while time.monotonic() < deadline:
        if not _group_exists(pid):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        yield min(CHILD_POLL_INTERVAL, remaining)
    raise TimeoutError(f"process group {pid} did not disappear during cleanup")


class ChildAcquisition:
    """Own spawn results before their adoption by the coordinator."""

    def __init__(self) -> None:
        self.process: ChildProcessHandle | None = None
        self.child: SupervisedProcess | None = None
        self.producer_quiescent = False
        self.rollback_confirmed = False
        self.termination_requested = False


class _OwnedChildPopen(subprocess.Popen[bytes]):
    """Register the raw child before initialization can publish its PID."""

    def __init__(
        self,
        owner: ChildAcquisition,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        environment: Mapping[str, str] | None,
    ) -> None:
        owner.process = self
        super().__init__(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )


class ChildSettlement(NamedTuple):
    producer_quiescent: bool
    group_disposed: bool
    observers_settled: bool
    pipes_closed: bool

    @property
    def confirmed(self) -> bool:
        return (
            self.producer_quiescent
            and self.group_disposed
            and self.observers_settled
            and self.pipes_closed
        )


def _poll_unreaped_process(process: ChildProcessHandle) -> int | None:
    """Observe genuine status while reserving the leader's group identity."""
    if process.returncode is not None:
        return process.returncode
    result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if result is None or result.si_pid == 0:
        return None
    if result.si_code == os.CLD_EXITED:
        return result.si_status
    if result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
        return -result.si_status
    raise RuntimeError(f"unexpected child wait status: {result.si_code}")


class SupervisedProcess:
    """Own a process group; observe the leader before explicitly reaping it."""

    def __init__(
        self, process: ChildProcessHandle, required_output_sentinels: Iterable[str] = ()
    ) -> None:
        self.process = process
        self.required_output_sentinels = RequiredOutputSentinels(required_output_sentinels)
        self.decoders = {
            name: OutputDecoder(name, self.required_output_sentinels)
            for name in ("stdout", "stderr")
        }
        self.output_finished: set[str] = set()
        self.readers: dict[str, AsyncInput] = {}
        self.tasks: list[asyncio.Task[ObservationFailed | None]] = []
        self._observed_returncode: int | None = None
        self.group_disposed = False
        self.termination_requested = False

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        if self._observed_returncode is not None:
            return self._observed_returncode
        return self.process.returncode

    def poll(self) -> int | None:
        if self._observed_returncode is not None:
            return self._observed_returncode
        self._observed_returncode = _poll_unreaped_process(self.process)
        return self._observed_returncode

    async def wait_for_exit(self) -> int:
        returncode = self.poll()
        if returncode is not None:
            return returncode
        try:
            descriptor = os.pidfd_open(self.pid)
        except OSError:
            # Older supported Linux kernels may not provide pidfds.
            while self.poll() is None:
                await asyncio.sleep(CHILD_POLL_INTERVAL)
        else:
            try:
                await readable(descriptor)
            finally:
                os.close(descriptor)
        returncode = self.poll()
        assert returncode is not None
        return returncode

    async def _wait_for_leader_exit(self) -> bool:
        try:
            async with asyncio.timeout(CHILD_TERM_TIMEOUT):
                await self.wait_for_exit()
        except TimeoutError:
            return False
        return True

    def _remaining_group_members(self) -> tuple[int, ...]:
        """Return observable non-leader members of the owned process group."""
        members: list[int] = []
        try:
            entries = Path("/proc").iterdir()
        except OSError:
            return ()
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == self.pid:
                continue
            try:
                fields = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
                if int(fields[2]) == self.pid:
                    members.append(int(entry.name))
            except (IndexError, OSError, ValueError):
                continue
        return tuple(members)

    def _warn_remaining_group_members(self) -> None:
        members = self._remaining_group_members()
        if members:
            message = (
                "warning: terminating remaining OpenOCD process-group members: "
                + ", ".join(map(str, members))
                + "\n"
            )
            try:
                descriptor = sys.stderr.fileno()
            except (AttributeError, io.UnsupportedOperation):
                sys.stderr.write(message)
                sys.stderr.flush()
                return
            was_blocking = os.get_blocking(descriptor)
            try:
                os.set_blocking(descriptor, False)
                with suppress(BlockingIOError):
                    os.write(descriptor, message.encode("utf-8"))
            finally:
                os.set_blocking(descriptor, was_blocking)

    async def terminate(self) -> None:
        if self.group_disposed:
            return
        errors = []
        if self.process.returncode is None:
            # Capture already observable natural status before signalling. Group
            # disposal still applies when descendants outlive that leader.
            try:
                observed = self.poll()
            except BaseException as exc:
                errors.append(exc)
                observed = None
            group_exists = True
            try:
                os.killpg(self.pid, signal.SIGTERM)
                if observed is None:
                    self.termination_requested = True
            except ProcessLookupError:
                group_exists = False
            except BaseException as exc:
                errors.append(exc)
            if group_exists:
                try:
                    await self._wait_for_leader_exit()
                except BaseException as exc:
                    errors.append(exc)
                try:
                    group_exists = _group_exists(self.pid)
                except BaseException as exc:
                    errors.append(exc)
                    group_exists = True
                if group_exists:
                    with suppress(BaseException):
                        self._warn_remaining_group_members()
                    try:
                        observed = self.poll()
                    except BaseException as exc:
                        errors.append(exc)
                        observed = None
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                        if observed is None:
                            self.termination_requested = True
                    except ProcessLookupError:
                        pass
                    except BaseException as exc:
                        errors.append(exc)
            try:
                # Reap only after group signalling; this wait has a finite budget.
                self._observed_returncode = self.process.wait(timeout=CHILD_REAP_TIMEOUT)
            except BaseException as exc:
                errors.append(exc)
        try:
            # An unreaped leader itself keeps the group observable. Once
            # reaped, only observe: its PID is no longer reserved for us.
            for delay in _group_exit_waits(self.pid):
                await asyncio.sleep(delay)
            self.group_disposed = True
        except BaseException as exc:
            errors.append(exc)
        raise_cleanup_errors(errors)

    def close_streams(self) -> None:
        errors = []
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except BaseException as exc:
                    errors.append(exc)
        raise_cleanup_errors(errors)


def rollback_spawned_process(
    process: ChildProcessHandle, ownership: ChildAcquisition | None = None
) -> None:
    errors: list[BaseException] = []
    try:
        observed = _poll_unreaped_process(process)
    except BaseException as error:
        errors.append(error)
        observed = None
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            if observed is None and ownership is not None:
                ownership.termination_requested = True
        except ProcessLookupError:
            pass
        except BaseException as error:
            errors.append(error)
    try:
        process.wait(timeout=CHILD_REAP_TIMEOUT)
    except BaseException as error:
        errors.append(error)
    try:
        for delay in _group_exit_waits(process.pid):
            time.sleep(delay)
    except BaseException as error:
        errors.append(error)
    for stream in (process.stdout, process.stderr):
        if stream is None or stream.closed:
            continue
        try:
            stream.close()
        except BaseException as error:
            errors.append(error)
    raise_cleanup_errors(errors)


def spawn_child(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    required_output_sentinels: Iterable[str] = (),
    ownership: ChildAcquisition | None = None,
) -> SupervisedProcess:
    owner = ownership if ownership is not None else ChildAcquisition()
    try:
        _OwnedChildPopen(
            owner,
            argv,
            cwd=cwd,
            environment=environment,
        )
        assert owner.process is not None
        owner.child = SupervisedProcess(owner.process, required_output_sentinels)
        return owner.child
    except BaseException as error:
        if owner.process is not None and getattr(owner.process, "pid", None) is None:
            # Pre-publication initialization failures retain Popen's cleanup;
            # the preregistered instance is not a usable child handle yet.
            owner.process = None
        if owner.process is not None:
            try:
                rollback_spawned_process(owner.process, owner)
                owner.rollback_confirmed = True
            except BaseException as cleanup_error:
                error.add_note(f"child ownership rollback also failed: {cleanup_error}")
                for note in tuple(getattr(cleanup_error, "__notes__", ())):
                    error.add_note(f"child ownership rollback detail: {note}")
        raise
    finally:
        owner.producer_quiescent = True


def check_required_paths(checks: Iterable[RequiredPath], *, workspace: str, address: str) -> None:
    for check in checks:
        path = materialize_path(check.path, workspace=workspace, address=address)
        if not path:
            raise ValueError("required remote path must not be empty")
        candidate = Path(path)
        valid = candidate.is_file() if check.kind == "file" else candidate.is_dir()
        if not valid:
            raise ValueError(f"required remote {check.kind} is missing: {candidate}")


def child_environment(request: StartRequest) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(dict(request.environment))
    return environment
